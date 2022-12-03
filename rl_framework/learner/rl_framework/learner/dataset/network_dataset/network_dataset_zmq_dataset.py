# -*- coding:utf-8 -*-
import multiprocessing
import os
import lz4.block
import tensorflow as tf
from rl_framework.learner.dataset.network_dataset.common.batch_process import (
    BatchProcess,
)
from rl_framework.learner.dataset.network_dataset import NetworkDatasetBase
from rl_framework.learner.dataset.network_dataset.common.sample_manager import MemBuffer
from rl_framework.mem_pool.zmq_mem_pool_server.zmq_mem_pool import ZMQMEMPOOL


class NetworkDataset(NetworkDatasetBase):
    def __init__(self, config_manager, AdapterClass):
        self.max_sample = config_manager.max_sample
        self.batch_size = config_manager.batch_size
        self.data_shapes = AdapterClass.get_data_shapes()
        self.use_fp16 = config_manager.use_fp16
        if self.use_fp16:
            self.data_size = 2 * int(self.data_shapes[0][0])
            self.data_type = tf.float16
        else:
            self.data_size = 4 * int(self.data_shapes[0][0])
            self.data_type = tf.float32
        self.init_index = config_manager.hvd_local_rank
        self.AdapterClass = AdapterClass
        self.membuffer = MemBuffer(
            config_manager.max_sample, self.data_shapes[0][0], self.use_fp16
        )

        self.batch_process = BatchProcess(
            self.batch_size,
            self.data_shapes[0][0],
            config_manager.batch_process,
            self.use_fp16,
        )

        server_ports = config_manager.ports
        self.port = server_ports[self.init_index]
        self.zmq_mem_pool = ZMQMEMPOOL(self.port)
        self.init_dataset = False

        for i in range(config_manager.sample_process):
            pid = multiprocessing.Process(target=self.enqueue_data, args=(i,))
            pid.daemon = True
            pid.start()

        self.batch_process.process(self.membuffer.get_sample)

    def _data_generator(self):
        while True:
            yield self.batch_process.get_batch_data()

    def get_next_batch(self):
        train_generator_worker_num = 2
        if not self.init_dataset:
            datasets = tf.data.Dataset.range(train_generator_worker_num).apply(
                tf.contrib.data.parallel_interleave(
                    lambda x: tf.data.Dataset.from_generator(
                        self._data_generator,
                        output_types=self.data_type,
                        output_shapes=tf.TensorShape(
                            [self.batch_size, self.data_shapes[0][0]]
                        ),
                    ),
                    cycle_length=train_generator_worker_num,
                    sloppy=True,
                )
            )
            datasets = datasets.prefetch(tf.contrib.data.AUTOTUNE)
            self.iterator = datasets.make_one_shot_iterator()
            self.init_dataset = True
        return [self.iterator.get_next()]

    def enqueue_data(self, process_index):
        print(
            "sample process learner_index:{} process_index:{} pid:{}".format(
                self.init_index, process_index, os.getpid()
            )
        )
        offline_rl_info_adapter = self.AdapterClass()
        while True:
            for sample in self.zmq_mem_pool.pull_samples():
                decompress_data = lz4.block.decompress(
                    sample, uncompressed_size=3 * 1024 * 1024
                )
                sample_list = offline_rl_info_adapter.deserialization(decompress_data)
                for sample in sample_list:
                    self.membuffer.append(sample)

    def get_recv_speed(self):
        return self.membuffer.get_speed()
