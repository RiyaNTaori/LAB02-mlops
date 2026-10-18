import functions_framework

import logging
import os
import traceback

from google.cloud import bigquery

import yaml

with open(os.path.join(os.path.dirname(__file__), "schemas.yaml")) as schema_file:
    config = yaml.load(schema_file, Loader=yaml.SafeLoader)

BQ_DATASET = os.getenv('BQ_DATASET', 'staging')
BQ = bigquery.Client()


def streaming(data):
    bucketname = data['bucket']
    print("Bucket name", bucketname)
    filename = data['name']
    print("File name", filename)
    timeCreated = data['timeCreated']
    print("Time Created", timeCreated)
    basename = os.path.basename(filename)
    try:
        # Longest name first so e.g. "housing_listings_v2" would win over "housing_listings".
        for table in sorted(config, key=lambda t: len(t['name']), reverse=True):
            tableName = table.get('name')
            if basename.startswith(tableName) or basename.startswith(tableName.replace('_', '-')):
                tableSchema = table.get('schema')
                _check_if_table_exists(tableName, tableSchema)
                tableFormat = table.get('format')
                if tableFormat == 'NEWLINE_DELIMITED_JSON':
                    _load_table_from_uri(bucketname, filename, tableSchema, tableName)
                return
        print("No table in schemas.yaml matches file %s, skipping." % filename)
    except Exception:
        print('Error streaming file. Cause: %s' % (traceback.format_exc()))
        raise


def _check_if_table_exists(tableName, tableSchema):
    table_id = BQ.dataset(BQ_DATASET).table(tableName)

    try:
        BQ.get_table(table_id)
    except Exception:
        logging.warning('Creating table: %s' % (tableName))
        schema = create_schema_from_yaml(tableSchema)
        table = bigquery.Table(table_id, schema=schema)
        table = BQ.create_table(table, exists_ok=True)
        print("Created table {}.{}.{}".format(table.project, table.dataset_id, table.table_id))


def _load_table_from_uri(bucket_name, file_name, tableSchema, tableName):
    uri = 'gs://%s/%s' % (bucket_name, file_name)
    table_id = BQ.dataset(BQ_DATASET).table(tableName)

    schema = create_schema_from_yaml(tableSchema)
    print(schema)

    # A fresh config per load; the original lab's trailing commas turned these into tuples.
    job_config = bigquery.LoadJobConfig(
        schema=schema,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
    )

    load_job = BQ.load_table_from_uri(
        uri,
        table_id,
        job_config=job_config,
    )

    load_job.result()
    print("Job finished. Loaded %s rows into %s.%s" % (load_job.output_rows, BQ_DATASET, tableName))


def create_schema_from_yaml(table_schema):
    schema = []
    for column in table_schema:
        fields = create_schema_from_yaml(column['fields']) if column['type'] == 'RECORD' else ()
        schemaField = bigquery.SchemaField(column['name'], column['type'], column['mode'], fields=fields)
        schema.append(schemaField)
    return schema


@functions_framework.cloud_event
def hello_gcs(cloud_event):
    data = cloud_event.data

    event_id = cloud_event["id"]
    event_type = cloud_event["type"]

    bucket = data["bucket"]
    name = data["name"]
    metageneration = data["metageneration"]
    timeCreated = data["timeCreated"]
    updated = data["updated"]

    print(f"Event ID: {event_id}")
    print(f"Event type: {event_type}")
    print(f"Bucket: {bucket}")
    print(f"File: {name}")
    print(f"Metageneration: {metageneration}")
    print(f"Created: {timeCreated}")
    print(f"Updated: {updated}")

    streaming(data)
