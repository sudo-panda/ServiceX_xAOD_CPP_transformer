# Copyright (c) 2019, IRIS-HEP
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
import json
import time

from servicex.transformer.servicex_adapter import ServiceXAdapter
from servicex.transformer.transformer_argument_parser import TransformerArgumentParser
from servicex.transformer.object_store_manager import ObjectStoreManager
from servicex.transformer.rabbit_mq_manager import RabbitMQManager
from servicex.transformer.uproot_events import UprootEvents
from servicex.transformer.uproot_transformer import UprootTransformer
from servicex.transformer.arrow_writer import ArrowWriter
import uproot
import os
import sys
import traceback

import logging
import timeit
import psutil
# from typing import NamedTuple
from collections import namedtuple


# How many bytes does an average awkward array cell take up. This is just
# a rule of thumb to calculate chunksize
avg_cell_size = 42
MAX_RETRIES = 3

messaging = None
object_store = None


def initialize_logging(request=None):
    """
    Get a logger and initialize it so that it outputs the correct format

    :param request: Request id to insert into log messages
    :return: logger with correct formatting that outputs to console
    """

    log = logging.getLogger()
    if 'INSTANCE_NAME' in os.environ:
        instance = os.environ['INSTANCE_NAME']
    else:
        instance = 'Unknown'
    formatter = logging.Formatter('%(levelname)s ' +
                                  "{} xaod_cpp_transformer {} ".format(instance, request) +
                                  '%(message)s')
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.setLevel(logging.INFO)
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    return log


def parse_output_logs(logfile):
    """
    Parse output from runner.sh and output appropriate log messages
    :param logfile: path to logfile
    :return:  None
    """
    total_events = 0
    total_bytes = 0
    successes = 0
    total_files = 0
    total_time = 0
    with open(logfile, 'r') as f:
        for line in f.readlines():
            if '---<' in line:
                # message is ('------< ', json_string)
                # want everything after the comma except for last character (')')
                mesg = line.split(',', 1)[1].strip()[:-1]
                # need to change ' to " to make message proper json
                body = json.loads(mesg.replace("'", '"'))
                events = body.get("total-events", 0)
                bytes_processed = body.get("total-bytes", 0)
                processing_time = body.get("total-time", 0)
                total_events += events
                total_bytes += bytes_processed
                total_time += processing_time
                total_files += 1
                status = body.get("status", "unknown")
                if status not in ("failure", "unknown"):
                    successes += 1
                logger.info("Processed {} ".format(body.get("file-path", "unknown file")) +
                            "events: {} ".format(events) +
                            "bytes: {} ".format(bytes_processed) +
                            "seconds: {} ".format(processing_time) +
                            "status: {}".format(status))
        logger.info("Total events: {}".format(total_events))
        logger.info("Total bytes: {}".format(total_bytes))
        logger.info("Total time: {}".format(total_time))
        logger.info("Total successes: {}/{}".format(successes, total_files))


# class TimeTuple(NamedTuple):
class TimeTuple(namedtuple("TimeTupleInit", ["user", "system", "idle"])):
    """
    Named tuple to store process time information.
    Immutable so values can't be accidentally altered after creation
    """
    # user: float
    # system: float
    # idle: float

    @property
    def total_time(self):
        """
        Return total time spent by process

        :return: sum of user, system, idle times
        """
        return self.user + self.system + self.idle


def get_process_info():
    """
    Get process information (just cpu, sys, idle times right now) and return it

    :return: TimeTuple with timing information
    """
    time_stats = psutil.cpu_times()
    return TimeTuple(user=time_stats.user, system=time_stats.system, idle=time_stats.idle)


def log_stats(startup_time, total_time, running_time=0.0):
    """
    Log statistics about transformer execution

    :param startup_time: time to initialize and run cpp transformer
    :param total_time:  total process times (sys, user, idle)
    :param running_time:  total time to run script
    :return: None
    """
    logger.info("Startup process times  user: {} sys: {} ".format(startup_time.user,
                                                                  startup_time.system) +
                "idle: {} total: {}".format(startup_time.idle, startup_time.total_time))
    logger.info("Total process times  user: {} sys: {} ".format(total_time.system, total_time.system) +
                "idle: {} total: {}".format(total_time.idle, total_time.total_time))
    logger.info("Total running time {}".format(running_time))


# noinspection PyUnusedLocal
def callback(channel, method, properties, body):
    transform_request = json.loads(body)
    _request_id = transform_request['request-id']
    _file_path = transform_request['file-path'].encode('ascii', 'ignore')
    _file_id = transform_request['file-id']
    _server_endpoint = transform_request['service-endpoint']
    _chunks = transform_request['chunk-size']
    servicex = ServiceXAdapter(_server_endpoint)

    servicex.post_status_update(file_id=_file_id,
                                status_code="start",
                                info="xAOD Transformer")

    tick = time.time()
    file_done = False
    file_retries = 0
    while not file_done:
        try:
            # Do the transform
            root_file = _file_path.replace('/', ':')
            output_path = '/home/atlas/' + root_file
            transform_single_file(_file_path, output_path, _chunks, servicex)

            tock = time.time()

            if object_store:
                object_store.upload_file(_request_id, root_file, output_path)
                os.remove(output_path)

            servicex.post_status_update(file_id=_file_id,
                                        status_code="complete",
                                        info="Total time " + str(round(tock - tick, 2)))

            servicex.put_file_complete(_file_path, _file_id, "success",
                                    num_messages=0,
                                    total_time=round(tock - tick, 2),
                                    total_events=0,
                                    total_bytes=0)
            file_done = True

        except Exception as error:
            file_retries += 1
            if file_retries == MAX_RETRIES:
                transform_request['error'] = str(error)
                channel.basic_publish(exchange='transformation_failures',
                                    routing_key=_request_id + '_errors',
                                    body=json.dumps(transform_request))
                servicex.put_file_complete(file_path=_file_path, file_id=_file_id,
                                        status='failure', num_messages=0, total_time=0,
                                        total_events=0, total_bytes=0)

                servicex.post_status_update(file_id=_file_id,
                                            status_code="failure",
                                            info="error: " + str(error))

                file_done = True
                exc_type, exc_value, exc_traceback = sys.exc_info()
                traceback.print_tb(exc_traceback, limit=20, file=sys.stdout)
                logger.exception("Received exception")
                print(exc_value)
            else:
                servicex.post_status_update(file_id=_file_id,
                                            status_code="retry",
                                            info="Try: " + str(file_retries) +
                                                 " error: " + str(error)[0:1024])

    channel.basic_ack(delivery_tag=method.delivery_tag)


def transform_single_file(file_path, output_path, chunks, servicex=None):
    logger.info("Transforming a single path: " + str(file_path) + " into " + output_path)
    # os.system("voms-proxy-info --all")
    r = os.system('bash /generated/runner.sh -r -d ' + file_path + ' -o ' + output_path + '| tee log.txt')
    parse_output_logs("log.txt")

    reason_bad = None
    if r != 0:
        reason_bad = "Error return from transformer: " + str(r)
    if (reason_bad is None) and not os.path.exists(output_path):
        reason_bad = "Output file " + output_path + " was not found"
    if reason_bad is not None:
        with open('log.txt', 'r') as f:
            errors = f.read()
            logger.error("Failed to transform input file {}: ".format(file_path) +
                         "{} -- errors: {}".format(reason_bad, errors))
            raise RuntimeError("Failed to transform input file {}: ".format(file_path) +
                               "{} -- errors: {}".format(reason_bad, errors))

            raise RuntimeError("Failed to transform input file " + file_path + ": " + reason_bad + ' -- errors: \n' + errors)

    if not object_store:
        flat_file = uproot.open(output_path)
        flat_tree_name = flat_file.keys()[0]
        attr_name_list = flat_file[flat_tree_name].keys()

        arrow_writer = ArrowWriter(file_format=args.result_format,
                                   object_store=object_store,
                                   messaging=messaging)
        # NB: We're converting the *output* ROOT file to Arrow arrays
        event_iterator = UprootEvents(file_path=output_path, tree_name=flat_tree_name,
                                      attr_name_list=attr_name_list, chunk_size=chunks)
        transformer = UprootTransformer(event_iterator)
        arrow_writer.write_branches_to_arrow(transformer=transformer, topic_name=args.request_id,
                                             file_id=None, request_id=args.request_id)
        logger.info("Kafka Timings: "+str(arrow_writer.messaging_timings))


def compile_code():
    # Have to use bash as the file runner.sh does not execute properly, despite its 'x'
    # bit set. This seems to be some vagary of a ConfigMap from k8, which is how we usually get
    # this file.
    r = os.system('bash /generated/runner.sh -c | tee log.txt')
    if r != 0:
        with open('log.txt', 'r') as f:
            errors = f.read()
            logger.error("Unable to compile the code - error return: " + str(r) + 'errors: \n' + errors)
            raise RuntimeError("Unable to compile the code - error return: " + str(r) + 'errors: \n' + errors)


if __name__ == "__main__":
    print("starting xaod_cpp_transformer")
    start_time = timeit.default_timer()
    parser = TransformerArgumentParser(description="xAOD CPP Transformer")
    args = parser.parse_args()

    logger = initialize_logging(args.request_id)

    if args.result_destination == 'kafka':
        logger.error("Kafka is no longer supported as a transport mechanism")
        sys.stderr.write("Kafka is no longer supported as a transport mechanism\n")
        sys.exit(1)
    elif not args.output_dir and args.result_destination == 'object-store':
        messaging = None
        object_store = ObjectStoreManager()

    compile_code()
    startup_time = get_process_info()

    if args.request_id and not args.path:
        print("A******")
        rabbitmq = RabbitMQManager(args.rabbit_uri, args.request_id, callback)
        print("B******")

    if args.path:
        print("1******")
        transform_single_file(args.path, args.output_dir)
        print("2******")
    total_time = get_process_info()
    stop_time = timeit.default_timer()
    log_stats(startup_time, total_time, running_time=(stop_time - start_time))
    print("finished xaod_cpp_transformer")
    print("******")