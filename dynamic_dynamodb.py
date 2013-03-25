#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Dynamic DynamoDB

Auto provisioning functionality for Amazon Web Service DynamoDB tables.


APACHE LICENSE 2.0
Copyright 2013 Sebastian Dahlgren

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import sys
import math
import time
import logging
import datetime
import argparse

from boto import dynamodb
from boto.ec2 import cloudwatch
from boto.exception import DynamoDBResponseError

from ConfigParser import SafeConfigParser


# pylint: disable=R0902
# pylint: disable=R0913
# pylint: disable=R0914
class DynamicDynamoDB:
    """ Class handling connections and scaling """
    cw_connection = None
    ddb_connection = None

    def __init__(self, region, table_name,
                reads_upper_threshold, reads_lower_threshold,
                increase_reads_with, decrease_reads_with,
                writes_upper_threshold, writes_lower_threshold,
                increase_writes_with, decrease_writes_with,
                min_provisioned_reads=None, max_provisioned_reads=None,
                min_provisioned_writes=None, max_provisioned_writes=None,
                check_interval=300, dry_run=True,
                aws_access_key_id=None, aws_secret_access_key=None):
        """ Constructor setting the basic configuration

        :type region: str
        :param region: AWS region to connect to
        :type table_name: str
        :param table_name: DynamoDB table name to use
        :type reads_upper_threshold: int
        :param reads_upper_threshold: Usage percent when we should scale up
        :type reads_lower_threshold: int
        :param reads_lower_threshold: Usage percent when we should scale down
        :type increase_reads_with: int
        :param increase_reads_with: How many percent to scale up reads with
        :type decrease_reads_with: int
        :param decrease_reads_with: How many percent to scale down reads with
        :type min_provisioned_reads: int
        :param min_provisioned_reads: Minimum number of provisioned reads
        :type max_provisioned_reads: int
        :param max_provisioned_reads: Maximum number of provisioned reads
        :type writes_upper_threshold: int
        :param writes_upper_threshold: Usage percent when we should scale up
        :type writes_lower_threshold: int
        :param writes_lower_threshold: Usage percent when we should scale down
        :type increase_writes_with: int
        :param increase_writes_with: How many percent to scale up writes with
        :type decrease_writes_with: int
        :param decrease_writes_with: How many percent to scale down writes with
        :type min_provisioned_writes: int
        :param min_provisioned_writes: Minimum number of provisioned writes
        :type max_provisioned_writes: int
        :param max_provisioned_writes: Maximum number of provisioned writes
        :type check_interval: int
        :param check_interval: How many seconds to wait between checks
        :type dry_run: bool
        :param dry_run: Set to False if we should make actual changes
        :type aws_access_key_id: str
        :param aws_access_key_id: AWS access key to use
        :type aws_secret_access_key: str
        :param aws_secret_access_key: AWS secret key to use
        """
        self.dry_run = dry_run

        #
        # Logging configuration
        #
        self.logger = logging.getLogger('dynamic-dynamodb')
        self.logger.setLevel(logging.DEBUG)
        if self.dry_run:
            stdout_formatter = logging.Formatter(
                '%(asctime)s - %(name)s - dryrun - %(levelname)s - %(message)s')
        else:
            stdout_formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(stdout_formatter)
        self.logger.addHandler(console_handler)

        #
        # Handel parameters
        #
        if int(increase_reads_with) > 100:
            self.logger.error(
                'You can not increase the table throughput with more '
                'than 100% at a time. Setting --increase-reads-with to 100.')
            increase_reads_with = 100

        if int(increase_writes_with) > 100:
            self.logger.error(
                'You can not increase the table throughput with more '
                'than 100% at a time. Setting --increase-writes-with to 100.')
            increase_writes_with = 100

        self.region = region
        self.table_name = table_name
        self.reads_upper_threshold = int(reads_upper_threshold)
        self.reads_lower_threshold = int(reads_lower_threshold)
        self.increase_reads_with = int(increase_reads_with)
        self.decrease_reads_with = int(decrease_reads_with)
        self.writes_upper_threshold = int(writes_upper_threshold)
        self.writes_lower_threshold = int(writes_lower_threshold)
        self.increase_writes_with = int(increase_writes_with)
        self.decrease_writes_with = int(decrease_writes_with)
        self.min_provisioned_reads = min_provisioned_reads
        self.max_provisioned_reads = max_provisioned_reads
        self.min_provisioned_writes = min_provisioned_writes
        self.max_provisioned_writes = max_provisioned_writes
        self.check_interval = int(check_interval)
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key

    def run(self):
        """ Public method for starting scaling """
        try:
            while True:
                self.ensure_proper_provisioning()
                self.logger.info('Waiting {0:d}s until checking again'.format(
                    self.check_interval))
                time.sleep(self.check_interval)
        except KeyboardInterrupt:
            self.logger.info('Caught termination signal. Exiting.')

    def ensure_proper_provisioning(self):
        """ Public method running once to check the current provisioning

        This method is called by run() in order to check provisioning over time
        """
        read_usage_percent = self._get_consumed_reads_percentage()
        write_usage_percent = self._get_consumed_writes_percentage()

        throughput = {
            'update_needed': False,
            'read_units': self._get_provisioned_read_units(),
            'write_units': self._get_provisioned_write_units()
        }

        # Check if we should update read provisioning
        if read_usage_percent == 0:
            self.logger.info('Scale down is not performed when usage is at 0%')
        elif read_usage_percent >= self.reads_upper_threshold:
            new_value = self._calculate_increase_reads(
                throughput['read_units'],
                self.increase_reads_with)
            throughput['update_needed'] = True
            throughput['read_units'] = new_value

        elif read_usage_percent <= self.reads_lower_threshold:
            new_value = self._calculate_decrease_reads(
                throughput['read_units'],
                self.increase_reads_with)
            throughput['update_needed'] = True
            throughput['read_units'] = new_value

        # Check if we should update write provisioning
        if write_usage_percent == 0:
            self.logger.info('Scale down is not performed when usage is at 0%')
        elif write_usage_percent >= self.writes_upper_threshold:
            new_value = self._calculate_increase_writes(
                throughput['write_units'],
                self.increase_reads_with)
            throughput['update_needed'] = True
            throughput['write_units'] = new_value
        elif write_usage_percent <= self.writes_lower_threshold:
            new_value = self._calculate_decrease_writes(
                throughput['write_units'],
                self.increase_reads_with)
            throughput['update_needed'] = True
            throughput['write_units'] = new_value

        # Handle throughput updates
        if throughput['update_needed']:
            self.logger.info(
                'Changing provisioning to {0:d} reads and {1:d} writes'.format(
                    throughput['read_units'],
                    throughput['write_units']))
            self._update_throughput(
                throughput['read_units'],
                throughput['write_units'])
        else:
            self.logger.info('No need to change provisioning')

    def _calculate_decrease_reads(self, original_provisioning, percent):
        """ Decrease the original_provisioning with percent %

        :type original_provisioning: int
        :param original_provisioning: The current provisioning
        :type percent: int
        :param percent: How many percent should we decrease with
        :returns: int -- New provisioning value
        """
        decrease = int(float(original_provisioning)*(float(percent)/100))
        if self.min_provisioned_reads:
            if decrease < self.min_provisioned_reads:
                return self.min_provisioned_reads
        return decrease

    def _calculate_increase_reads(self, original_provisioning, percent):
        """ Increase the original_provisioning with percent %

        :type original_provisioning: int
        :param original_provisioning: The current provisioning
        :type percent: int
        :param percent: How many percent should we increase with
        :returns: int -- New provisioning value
        """
        increase = int(float(original_provisioning)*(float(percent)/100+1))
        if self.max_provisioned_reads:
            if increase > self.max_provisioned_reads:
                return self.max_provisioned_reads
        return increase

    def _calculate_decrease_writes(self, original_provisioning, percent):
        """ Decrease the original_provisioning with percent %

        :type original_provisioning: int
        :param original_provisioning: The current provisioning
        :type percent: int
        :param percent: How many percent should we decrease with
        :returns: int -- New provisioning value
        """
        decrease = int(float(original_provisioning)*(float(percent)/100))
        if self.min_provisioned_writes:
            if decrease < self.min_provisioned_writes:
                return self.min_provisioned_writes
        return decrease

    def _calculate_increase_writes(self, original_provisioning, percent):
        """ Increase the original_provisioning with percent %

        :type original_provisioning: int
        :param original_provisioning: The current provisioning
        :type percent: int
        :param percent: How many percent should we increase with
        :returns: int -- New provisioning value
        """
        increase = int(float(original_provisioning)*(float(percent)/100+1))
        if self.max_provisioned_writes:
            if increase > self.max_provisioned_writes:
                return self.max_provisioned_writes
        return increase

    def _ensure_cloudwatch_connection(self):
        """ Make sure that we have a CloudWatch connection """
        if not self.cw_connection:
            if self.aws_access_key_id and self.aws_secret_access_key:
                self.cw_connection = cloudwatch.connect_to_region(
                    self.region,
                    aws_access_key_id=self.aws_access_key_id,
                    aws_secret_access_key=self.aws_secret_access_key)
            else:
                self.cw_connection = cloudwatch.connect_to_region(self.region)

    def _ensure_dynamodb_connection(self):
        """ Make sure that we have a CloudWatch connection """
        if not self.ddb_connection:
            if self.aws_access_key_id and self.aws_secret_access_key:
                self.ddb_connection = dynamodb.connect_to_region(
                    self.region,
                    aws_access_key_id=self.aws_access_key_id,
                    aws_secret_access_key=self.aws_secret_access_key)
            else:
                self.ddb_connection = dynamodb.connect_to_region(self.region)

    def _get_consumed_reads_percentage(self):
        """ Get the percentage of consumed reads

        :returns: int -- Percentage of consumed reads
        """
        self._ensure_cloudwatch_connection()
        self._ensure_dynamodb_connection()

        table = self.ddb_connection.get_table(self.table_name)

        metrics = self.cw_connection.get_metric_statistics(
            300,
            datetime.datetime.utcnow()-datetime.timedelta(minutes=15),
            datetime.datetime.utcnow()-datetime.timedelta(minutes=10),
            'ConsumedReadCapacityUnits',
            'AWS/DynamoDB',
            ['Sum'],
            dimensions={'TableName': self.table_name},
            unit=None)

        if len(metrics) == 0:
            consumed_reads = 0
        else:
            consumed_reads = int(math.ceil(float(metrics[0]['Sum'])/float(300)))

        consumed_reads_percent = int(math.ceil(
            float(consumed_reads) / float(table.read_units) * 100))
        self.logger.info('Consumed reads: {0:d}'.format(consumed_reads))
        self.logger.info('Provisioned reads: {0:d}'.format(table.read_units))
        self.logger.info('Read usage: {0:d}%'.format(consumed_reads_percent))
        return consumed_reads_percent

    def _get_consumed_writes_percentage(self):
        """ Get the percentage of consumed writes

        :returns: int -- Percentage of consumed writes
        """
        self._ensure_cloudwatch_connection()
        self._ensure_dynamodb_connection()

        table = self.ddb_connection.get_table(self.table_name)

        metrics = self.cw_connection.get_metric_statistics(
            300,
            datetime.datetime.utcnow()-datetime.timedelta(minutes=15),
            datetime.datetime.utcnow()-datetime.timedelta(minutes=10),
            'ConsumedWriteCapacityUnits',
            'AWS/DynamoDB',
            ['Sum'],
            dimensions={'TableName': self.table_name},
            unit=None)

        if len(metrics) == 0:
            consumed_writes = 0
        else:
            consumed_writes = int(math.ceil(
                float(metrics[0]['Sum'])/float(300)))

        consumed_writes_percent = int(math.ceil(
            float(consumed_writes) / float(table.write_units) * 100))
        self.logger.info('Consumed writes: {0:d}'.format(consumed_writes))
        self.logger.info('Provisioned writes: {0:d}'.format(table.write_units))
        self.logger.info('Write usage: {0:d}%'.format(consumed_writes_percent))
        return consumed_writes_percent

    def _get_provisioned_read_units(self):
        """ Get the provisioned read units for the table

        :returns: int -- Provisioned read units
        """
        self._ensure_dynamodb_connection()
        table = self.ddb_connection.get_table(self.table_name)
        return int(table.read_units)

    def _get_provisioned_write_units(self):
        """ Get the provisioned write units for the table

        :returns: int -- Provisioned write units
        """
        self._ensure_dynamodb_connection()
        table = self.ddb_connection.get_table(self.table_name)
        return int(table.write_units)

    def _update_throughput(self, read_units, write_units):
        """ Update throughput on the DynamoDB table

        :type read_units: int
        :param read_units: New read unit provisioning
        :type write_units: int
        :param write_units: New write unit provisioning
        """
        self._ensure_dynamodb_connection()
        table = self.ddb_connection.get_table(self.table_name)

        if table.status != 'ACTIVE':
            self.logger.warning(
                'Not performing throughput changes when table '
                'is in {0} state'.format(table.status))

        if not self.dry_run:
            try:
                table.update_throughput(int(read_units), int(write_units))
                self.logger.info('Provisioning updated')
            except DynamoDBResponseError as error:
                dynamodb_error = error.body['__type'].rsplit('#', 1)[1]
                if dynamodb_error == 'LimitExceededException':
                    self.logger.warning(
                        'Scaling limit exeeded. '
                        'The table can only be scaled down twice per day.')
                else:
                    raise



def main():
    """ Main function handling option parsing etc """
    parser = argparse.ArgumentParser(
        description='Dynamic DynamoDB - Auto provisioning AWS DynamoDB')
    parser.add_argument('-c', '--config',
        help='Read configuration from a configuration file')
    parser.add_argument('--dry-run',
        action='store_true',
        help='Run without making any changes to your DynamoDB table')
    parser.add_argument('--check-interval',
        type=int,
        default=300,
        help="""How many seconds should we wait between
                the checks (default: 300)""")
    parser.add_argument('--aws-access-key-id',
        default=None,
        help="Override Boto configuration with the following AWS access key")
    parser.add_argument('--aws-secret-access-key',
        default=None,
        help="Override Boto configuration with the following AWS secret key")
    dynamodb_ag = parser.add_argument_group('DynamoDB settings')
    dynamodb_ag.add_argument('-r', '--region',
        default='us-east-1',
        help='AWS region to operate in (default: us-east-1')
    dynamodb_ag.add_argument('-t', '--table-name',
        required=True,
        help='How many percent should we decrease the read units with?')
    r_scaling_ag = parser.add_argument_group('Read units scaling properties')
    r_scaling_ag.add_argument('--reads-upper-threshold',
        default=90,
        type=int,
        help="""Scale up the reads with --increase-reads-with percent if
                the currently consumed read units reaches this many
                percent (default: 90)""")
    r_scaling_ag.add_argument('--reads-lower-threshold',
        default=30,
        type=int,
        help="""Scale down the reads with --decrease-reads-with percent if the
                currently consumed read units is as low as this
                percentage (default: 30)""")
    r_scaling_ag.add_argument('--increase-reads-with',
        default=50,
        type=int,
        help="""How many percent should we increase the read
                units with? (default: 50, max: 100)""")
    r_scaling_ag.add_argument('--decrease-reads-with',
        default=50,
        type=int,
        help="""How many percent should we decrease the
                read units with? (default: 50)""")
    r_scaling_ag.add_argument('--min-provisioned-reads',
        type=int,
        help="""Minimum number of provisioned reads""")
    r_scaling_ag.add_argument('--max-provisioned-reads',
        type=int,
        help="""Maximum number of provisioned reads""")
    w_scaling_ag = parser.add_argument_group('Write units scaling properties')
    w_scaling_ag.add_argument('--writes-upper-threshold',
        default=90,
        type=int,
        help="""Scale up the writes with --increase-writes-with percent
                if the currently consumed write units reaches this
                many percent (default: 90)""")
    w_scaling_ag.add_argument('--writes-lower-threshold',
        default=30,
        type=int,
        help="""Scale down the writes with --decrease-writes-with percent
                if the currently consumed write units is as low as this
                percentage (default: 30)""")
    w_scaling_ag.add_argument('--increase-writes-with',
        default=50,
        type=int,
        help="""How many percent should we increase the write
                units with? (default: 50, max: 100)""")
    w_scaling_ag.add_argument('--decrease-writes-with',
        default=50,
        type=int,
        help="""How many percent should we decrease the write
                units with? (default: 50)""")
    w_scaling_ag.add_argument('--min-provisioned-writes',
        type=int,
        help="""Minimum number of provisioned writes""")
    w_scaling_ag.add_argument('--max-provisioned-writes',
        type=int,
        help="""Maximum number of provisioned writes""")
    args = parser.parse_args()

    if args.aws_access_key_id and not args.aws_secret_access_key:
        print ('Both --aws-access-key-id and --aws-secret-access-key must '
            'be specified.')
        parser.print_help()
        sys.exit(1)

    if args.config:
        # Read the configuration file
        config = SafeConfigParser()
        config.optionxform = lambda option: option
        config.read(args.config)

        region = config.get('global', 'region')
        check_interval = config.get('global', 'check-interval')
        aws_access_key_id = config.get('global', 'aws-access-key-id')
        aws_secret_access_key = config.get('global', 'aws-secret-access-key-id')

        # Find the first table definition
        for current_section in config.sections():
            current_section = current_section.lsplit(':', 1)
            if current_section[0] != 'table':
                continue
            section = current_section[1].strip()
            break

        reads_upper_threshold = config.get(section, 'reads-upper-threshold')
        reads_lower_threshold = config.get(section, 'reads-lower-threshold')
        increase_reads_with = config.get(section, 'increase-reads-with')
        decrease_reads_with = config.get(section, 'decrease-reads-with')
        writes_upper_threshold = config.get(section, 'writes-upper-threshold')
        writes_lower_threshold = config.get(section, 'writes-lower-threshold')
        increase_writes_with = config.get(section, 'increase-writes-with')
        decrease_writes_with = config.get(section, 'decrease-writes-with')
        min_provisioned_reads = config.get(section, 'min-provisioned-reads')
        max_provisioned_reads = config.get(section, 'max-provisioned-reads')
        min_provisioned_writes = config.get(section, 'min-provisioned-writes')
        max_provisioned_writes = config.get(section, 'max-provisioned-writes')

    else:
        # Handle command line arguments
        region = args.region
        table_name = args.table_name
        reads_upper_threshold = args.reads_upper_threshold
        reads_lower_threshold = args.reads_lower_threshold
        increase_reads_with = args.increase_reads_with
        decrease_reads_with = args.decrease_reads_with
        writes_upper_threshold = args.writes_upper_threshold
        writes_lower_threshold = args.writes_lower_threshold
        increase_writes_with = args.increase_writes_with
        decrease_writes_with = args.decrease_writes_with
        min_provisioned_reads = args.min_provisioned_reads
        max_provisioned_reads = args.max_provisioned_reads
        min_provisioned_writes = args.min_provisioned_writes
        max_provisioned_writes = args.max_provisioned_writes
        check_interval = args.check_interval
        dry_run = args.dry_run
        aws_access_key_id = args.aws_access_key_id
        aws_secret_access_key = args.aws_secret_access_key

    dynamic_ddb = DynamicDynamoDB(
        region,
        table_name,
        reads_upper_threshold,
        reads_lower_threshold,
        increase_reads_with,
        decrease_reads_with,
        writes_upper_threshold,
        writes_lower_threshold,
        increase_writes_with,
        decrease_writes_with,
        min_provisioned_reads,
        max_provisioned_reads,
        min_provisioned_writes,
        max_provisioned_writes,
        check_interval=check_interval,
        dry_run=dry_run,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key)
    dynamic_ddb.run()

if __name__ == '__main__':
    main()
