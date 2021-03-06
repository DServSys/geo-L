#!/usr/bin/env python
# -*- coding: utf-8 -*-

from gzip import GzipFile
from io import BytesIO, StringIO
from logging import INFO
from more_itertools import consecutive_groups
from pandas import DataFrame, read_csv

import psycopg2
import time


class Cache:
    def __init__(self, logger, config, sparql, type):
        self.config = config
        self.sparql = sparql
        self.type = type

        self.info_logger = logger

    def create_cache_file(self):
        connection = psycopg2.connect(self.config.get_database_string())
        cursor = connection.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS {}(\"{}\" VARCHAR, \"{}\" VARCHAR, server_offset BIGINT, geo GEOMETRY)".format(
            'public.table_' + self.sparql.query_hash, self.config.get_var_uri(self.type), self.config.get_var_shape(self.type)))
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_geo_{} ON {} USING GIST(geo);".format(
            self.sparql.query_hash, 'table_' + self.sparql.query_hash))
        connection.commit()
        cursor.close()

        result = self.sparql.query(0)
        csv_result = StringIO(result.decode('utf-8'))
        self.insert_file(connection, csv_result)

        connection.close()

    def insert_file(self, connection, result):
        data_frame = read_csv(result)
        data_frame_column_headers = list(data_frame)
        column_headers = [self.config.get_var_uri(self.type), self.config.get_var_shape(self.type)]
        #print("column_headers",column_headers)
        for data_frame_column_header in data_frame_column_headers:
            if data_frame_column_header not in column_headers:
                data_frame.drop(data_frame_column_header, 1, inplace=True)

        output = StringIO()
        data_frame.to_csv(output, sep=';', index=False)
        output.seek(0)
        data_frame_column_headers = list(data_frame)  # TODO: Check if length is 3 and throw exception if not
        #print("data_frame_column_headers",data_frame_column_headers)
        cursor = connection.cursor()
        
        # first create a temp table and then insert only records whose keys do not yet exist, to avoid duplicates
        cursor.execute("CREATE TABLE IF NOT EXISTS {} AS SELECT * FROM {} WHERE false".format('temp_' + self.sparql.query_hash,'table_'+self.sparql.query_hash ))
       
        cursor.copy_expert(sql="COPY {} (\"{}\", \"{}\") FROM STDIN WITH CSV HEADER DELIMITER AS ';'".format(
            'temp_' + self.sparql.query_hash, data_frame_column_headers[0], data_frame_column_headers[1]), file=output)
        
         
        cursor.execute("""
         INSERT INTO {} 
                SELECT * FROM {}
                WHERE NOT EXISTS (SELECT 1 FROM {} WHERE {}.{}={}.{});""".format('table_'+self.sparql.query_hash,  'temp_'+self.sparql.query_hash, 'table_'+self.sparql.query_hash, 'table_'+self.sparql.query_hash, self.config.get_var_uri(self.type), 'temp_'+self.sparql.query_hash ,self.config.get_var_uri(self.type) ))
       
        cursor.execute("DELETE FROM {};".format('temp_'+self.sparql.query_hash))

        if self.config.get_geo_coding(self.type):
            print('geo')
            cursor.execute("""
            UPDATE {} 
            SET geo = ST_Transform(ST_GeomFromText("{}",{}),4326)
            WHERE geo IS NULL AND "{}" NOT LIKE '%EMPTY' AND "{}" NOT LIKE '%nan%' AND ST_ISVALID(ST_Transform(ST_GeomFromText("{}",{}),4326))""".format(
                'table_' + self.sparql.query_hash, self.config.get_var_shape(self.type), self.config.get_geo_coding(self.type),
                self.config.get_var_shape(self.type), self.config.get_var_shape(self.type), self.config.get_var_shape(self.type),
                self.config.get_geo_coding(self.type)))
        else:
            print("no geo")
            cursor.execute("""
            UPDATE {} 
            SET geo = ST_GeomFromText("{}") 
            WHERE geo IS NULL AND "{}" NOT LIKE '%EMPTY' AND "{}" NOT LIKE '%nan%' AND ST_ISVALID(ST_GeomFromText("{}"))""".format(
                'table_' + self.sparql.query_hash, self.config.get_var_shape(self.type),
                self.config.get_var_shape(self.type), self.config.get_var_shape(self.type), self.config.get_var_shape(self.type)))
        connection.commit()
        cursor.close()

    # INTERNET

    def create_cache(self):
        start = time.time()
        offset = self.config.get_offset(self.type)
        limit = self.config.get_limit(self.type)
        chunksize = self.config.get_chunksize(self.type)

        connection = psycopg2.connect(self.config.get_database_string())
        cursor = connection.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS {}(\"{}\" VARCHAR, \"{}\" VARCHAR, server_offset BIGINT, geo GEOMETRY)".format(
            'public.table_' + self.sparql.query_hash, self.config.get_var_uri(self.type), self.config.get_var_shape(self.type)))
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_geo_{} ON {} USING GIST(geo);".format(
            self.sparql.query_hash, 'table_' + self.sparql.query_hash))
        connection.commit()
        cursor.close()

        self.info_logger.logger.log(INFO, "Checking {} cache".format(self.type))

        new_data = False
        min_offset = offset
        min_server_offset, max_server_offset = self.find_min_max_server_offset(connection)

        if limit > 0:
            max_offset = offset + limit - 1

            if (min_server_offset != None and min_server_offset > 0 and max_offset < min_server_offset) or (max_server_offset == None):
                self.info_logger.logger.log(INFO, "Cache is missing data, downloading missing data...")
                self.download_results(connection, offset, limit, chunksize)
                new_data = True
            elif min_offset > max_server_offset:
                more_results = self.check_more_results(min_offset + 1)

                if more_results:
                    self.info_logger.logger.log(INFO, "Cache is missing data, downloading missing data...")
                    self.download_results(connection, offset, limit, chunksize)
                    new_data = True
            else:
                intervals = []

                if offset < min_server_offset:
                    intervals.append((offset, min_server_offset - 1))
                    min_offset = min_server_offset

                if max_offset > max_server_offset:
                    more_results = self.check_more_results(max_server_offset + 1)

                    if more_results:
                        intervals.append((max_server_offset + 1, max_offset))

                    max_offset = max_server_offset

                missing_limit = max_offset - min_offset + 1

                missing_intervals = self.find_missing_data(connection, min_offset, missing_limit)
                missing_intervals = sorted(missing_intervals + intervals)

                if len(missing_intervals) > 0:
                    self.info_logger.logger.log(INFO, "Cache is missing data, downloading missing data...")

                    for interval in missing_intervals:
                        interval_offset = interval[0]
                        interval_limit = 1

                        if len(interval) > 1:
                            interval_limit = interval[1] - interval_offset + 1

                        self.download_results(connection, interval_offset, interval_limit, chunksize)

                    new_data = True
        else:
            if max_server_offset == None:
                self.info_logger.logger.log(INFO, "Cache is missing data, downloading missing data...")
                self.download_results(connection, offset, limit, chunksize)
                new_data = True
            elif min_offset > max_server_offset:
                more_results = self.check_more_results(min_offset + 1)

                if more_results:
                    self.info_logger.logger.log(INFO, "Cache is missing data, downloading missing data...")
                    self.download_results(connection, offset, limit, chunksize)
                    new_data = True
            elif min_offset < min_server_offset:
                missing_interval = [(min_offset, min_server_offset - 1)]
                missing_intervals = self.find_missing_data(connection, min_offset, missing_limit)
                missing_intervals = missing_interval + missing_intervals
                more_results = self.check_more_results(max_server_offset + 1)

                if more_results:
                    missing_intervals + [(max_server_offset + 1,)]

                self.info_logger.logger.log(INFO, "Cache is missing data, downloading missing data...")

                for interval in missing_intervals:
                    interval_offset = interval[0]
                    interval_limit = -1

                    if len(interval) > 1:
                        interval_limit = interval[1] - interval_offset + 1

                    self.download_results(connection, interval_offset, interval_limit, chunksize)

                new_data = True
            else:
                missing_intervals = self.find_missing_data(connection, min_offset, max_server_offset)
                more_results = self.check_more_results(max_server_offset + 1)

                if more_results:
                    missing_intervals += [(max_server_offset + 1,)]

                if len(missing_intervals) > 0:
                    self.info_logger.logger.log(INFO, "Cache is missing data, downloading missing data...")

                    for interval in missing_intervals:
                        interval_offset = interval[0]
                        interval_limit = -1

                        if len(interval) > 1:
                            interval_limit = interval[1] - interval_offset + 1

                        self.download_results(connection, interval_offset, interval_limit, chunksize)

                    new_data = True

        if new_data:
            end = time.time()
            self.info_logger.logger.log(INFO, "Retrieving statements took {}s".format(round(end - start, 4)))
        else:
            self.info_logger.logger.log(INFO, "Data already cached..")

        invalid_geometries_count = self.count_invalid_geometries(connection)
        self.info_logger.logger.log(INFO, "{} invalid geometries in {}".format(invalid_geometries_count, self.type))

        connection.close()

    def download_results(self, connection, offset, limit, chunksize):
        start_offset = offset
        run = True

        while(run):
            current_max = offset + chunksize

            if limit > 0 and offset + chunksize >= start_offset + limit:
                chunksize = start_offset + limit - offset
                current_max = start_offset + limit
                run = False

            self.info_logger.logger.log(INFO, "Getting statements from {} to {}".format(offset, current_max))
            result = self.sparql.query(offset, chunksize)  # TODO: Retry if 502 (Exception and retry counter)
            result_info = result.info()

            if 'x-sparql-maxrows' in result_info:
                max_chunksize_server = int(result_info['x-sparql-maxrows'])

                if max_chunksize_server and max_chunksize_server < chunksize:
                    chunksize = max_chunksize_server
                    self.info_logger.logger.log(
                        INFO, "Max server rows is smaller than chunksize, new chunksize is {}".format(max_chunksize_server))

            if 'content-encoding' in result_info and result_info['content-encoding'] == 'gzip':
                csv_result = self.gunzip_response(result)
            else:
                csv_result = StringIO(result.convert().decode('utf-8'))

            result.response.close()
            size = self.insert(connection, csv_result, offset, chunksize)

            if size < chunksize:
                break

            offset = offset + chunksize

    def insert(self, connection, result, offset, chunksize):
        data_frame = read_csv(result)
        data_frame['server_offset'] = range(offset, offset + len(data_frame))
        data_frame_column_headers = list(data_frame)
        column_headers = [self.config.get_var_uri(self.type), self.config.get_var_shape(self.type), 'server_offset']

        for data_frame_column_header in data_frame_column_headers:
            if data_frame_column_header not in column_headers:
                data_frame.drop(data_frame_column_header, 1, inplace=True)

        size = len(data_frame)

        output = StringIO()
        data_frame.to_csv(output, sep=';', index=False)
        output.seek(0)
        data_frame_column_headers = list(data_frame)  # TODO: Check if length is 3 and throw exception if not

        cursor = connection.cursor()
        cursor.copy_expert(sql="COPY {} (\"{}\", \"{}\", \"{}\") FROM STDIN WITH CSV HEADER DELIMITER AS ';'".format(
            'table_' + self.sparql.query_hash, data_frame_column_headers[0], data_frame_column_headers[1], data_frame_column_headers[2]),
            file=output)

        if self.config.get_geo_coding(self.type):
            cursor.execute("""
            UPDATE {} 
            SET geo = ST_Transform(ST_GeomFromText("{}",{}),4326)
            WHERE geo IS NULL AND "{}" NOT LIKE '%EMPTY' AND "{}" NOT LIKE '%nan%' AND ST_ISVALID(ST_Transform(ST_GeomFromText("{}",{}),4326))""".format(
                'table_' + self.sparql.query_hash, self.config.get_var_shape(self.type), self.config.get_geo_coding(self.type),
                self.config.get_var_shape(self.type), self.config.get_var_shape(self.type), self.config.get_var_shape(self.type),
                self.config.get_geo_coding(self.type)))
        else:
            cursor.execute("""
            UPDATE {} 
            SET geo = ST_GeomFromText("{}") 
            WHERE geo IS NULL AND "{}" NOT LIKE '%EMPTY' AND "{}" NOT LIKE '%nan%' AND ST_ISVALID(ST_GeomFromText("{}"))""".format(
                'table_' + self.sparql.query_hash, self.config.get_var_shape(self.type),
                self.config.get_var_shape(self.type), self.config.get_var_shape(self.type), self.config.get_var_shape(self.type)))
        connection.commit()
        cursor.close()

        return size

    def gunzip_response(self, response):
        buffer = BytesIO()
        buffer.write(response.response.read())
        buffer.seek(0)

        with GzipFile(fileobj=buffer, mode='rb') as unzipped:
            result = unzipped.read()
            return StringIO(result.decode('utf-8'))

    def find_min_max_server_offset(self, connection):
        cursor = connection.cursor()
        cursor.execute("SELECT MIN(server_offset), MAX(server_offset) FROM {}".format('table_' + self.sparql.query_hash))
        result = cursor.fetchone()
        cursor.close()

        return result[0], result[1]

    def find_missing_data(self, connection, offset, limit):
        cursor = connection.cursor()
        cursor.execute("""
        SELECT s.i AS missing 
        FROM generate_series({}, {}) s(i) 
        WHERE NOT EXISTS (
            SELECT 1 FROM {} WHERE server_offset = s.i 
        ) 
        ORDER BY missing
        """.format(offset, offset + limit - 1, 'table_' + self.sparql.query_hash))
        result = cursor.fetchall()
        cursor.close()

        missing_values = [x[0] for x in result]
        missing_intervals = list(self.find_ranges(missing_values))

        return missing_intervals

    def find_ranges(self, iterable):
        for group in consecutive_groups(iterable):
            group = list(group)
            if len(group) == 1:
                yield group[0]
            else:
                yield group[0], group[-1]

    def check_more_results(self, offset):
        result = self.sparql.query(offset, 1)
        result_info = result.info()

        if 'content-encoding' in result_info and result_info['content-encoding'] == 'gzip':
            csv_result = self.gunzip_response(result)
        else:
            csv_result = StringIO(result.convert().decode('utf-8'))

        data_frame = read_csv(csv_result)
        return len(data_frame) == 1

    def count_invalid_geometries(self, connection):
        offset = self.config.get_offset(self.type)
        limit = self.config.get_limit(self.type)
        query = "SELECT COUNT(*) AS count FROM {} WHERE geo IS NULL".format('table_' + self.sparql.query_hash)

        if limit > 0:
            query += " AND server_offset BETWEEN {} AND {}".format(offset, limit)
        else:
            query += " AND server_offset >= {}".format(offset)

        cursor = connection.cursor()
        cursor.execute(query)
        result = cursor.fetchone()
        cursor.close()

        return result[0]
