"""Where SaansSetu keeps things between runs.

Everything is a JSON document stored under (kind, key, sort). Two stores share that one interface:

    MemoryStore   in the process. Tests, replay and the local dev server use it.
    DynamoStore   one DynamoDB table: partition key 'pk' (string), sort key 'sk' (string), TTL attribute 'ttl'.

What is kept (kind, key, sort):
    aq        station_id   hour "YYYY-MM-DDTHH:00"      one hourly pollutant reading
    wx        station_id   hour                         one hour of weather
    run       town_id      run time "YYYY-MM-DDTHH:MM"  every run document, kept a week
    latest    town_id      "run"                        the newest run document
    morning   town_id      date                         the first full run at or after the alert hour: the day's call
    alert     town_id      date                         the alert sent that morning. Its presence means "already sent".
    sub       town_id      chat_id                      a Telegram subscriber
    town      "all"        town_id                      one line per town for the town switcher
    config    town_id      "current"                    what the dashboard needs to draw the town
    check     town_id      date                         that morning's forecast against what happened
    accuracy  town_id      "current"                    the accuracy panel
    evidence  town_id      "current"                    wind-direction table for the polar plot
    replay    town_id      "<date>T<HH:MM>"             run documents of a replayed morning

This module imports neither pandas nor the models, so the API Lambda stays small and starts fast.
"""
import json
import time

AQ, WX, RUN, LATEST, MORNING, ALERT = "aq", "wx", "run", "latest", "morning", "alert"
SUB, TOWN, CONFIG, CHECK, ACCURACY, REPLAY = "sub", "town", "config", "check", "accuracy", "replay"
EVIDENCE = "evidence"
READING_TTL_DAYS, RUN_TTL_DAYS = 20, 7
MAX_ITEM_BYTES = 380_000   # DynamoDB items stop at 400 KB


def encode(doc):
    """JSON text. NaN and numpy or pandas types are refused here, where the mistake is easy to find."""
    return json.dumps(doc, ensure_ascii=False, allow_nan=False)


class MemoryStore:
    def __init__(self):
        self._data = {}    # (kind, key) -> {sort: json text}
        self.ttl = {}      # (kind, key, sort) -> days, recorded so tests can check it

    def put(self, kind, key, sort, doc, ttl_days=None):
        self._data.setdefault((kind, str(key)), {})[str(sort)] = encode(doc)
        self.ttl[(kind, str(key), str(sort))] = ttl_days

    def put_many(self, kind, key, docs, ttl_days=None):
        for sort, doc in docs.items():
            self.put(kind, key, sort, doc, ttl_days)

    def put_if_absent(self, kind, key, sort, doc, ttl_days=None):
        if str(sort) in self._data.get((kind, str(key)), {}):
            return False
        self.put(kind, key, sort, doc, ttl_days)
        return True

    def get(self, kind, key, sort):
        text = self._data.get((kind, str(key)), {}).get(str(sort))
        return None if text is None else json.loads(text)

    def query(self, kind, key, start=None, end=None, newest_first=False, limit=None):
        """[(sort, doc)] with start <= sort <= end, in sort order."""
        rows = self._data.get((kind, str(key)), {})
        sorts = sorted(s for s in rows if (start is None or s >= str(start)) and (end is None or s <= str(end)))
        if newest_first:
            sorts.reverse()
        return [(s, json.loads(rows[s])) for s in sorts[:limit]]

    def delete(self, kind, key, sort):
        self._data.get((kind, str(key)), {}).pop(str(sort), None)


class DynamoStore:
    def __init__(self, table_name, resource=None):
        import boto3   # imported here so the rest of the code runs without AWS installed
        self._table = (resource or boto3.resource("dynamodb")).Table(table_name)

    @staticmethod
    def _item(kind, key, sort, doc, ttl_days):
        text = encode(doc)
        size = len(text.encode("utf-8"))
        if size > MAX_ITEM_BYTES:
            raise ValueError(f"{kind}/{key}/{sort} is {size:,} bytes; one DynamoDB item holds at most 400 KB")
        item = {"pk": f"{kind}#{key}", "sk": str(sort), "doc": text}
        if ttl_days:
            item["ttl"] = int(time.time() + ttl_days * 86400)
        return item

    def put(self, kind, key, sort, doc, ttl_days=None):
        self._table.put_item(Item=self._item(kind, key, sort, doc, ttl_days))

    def put_many(self, kind, key, docs, ttl_days=None):
        with self._table.batch_writer(overwrite_by_pkeys=["pk", "sk"]) as batch:
            for sort, doc in docs.items():
                batch.put_item(Item=self._item(kind, key, sort, doc, ttl_days))

    def put_if_absent(self, kind, key, sort, doc, ttl_days=None):
        from botocore.exceptions import ClientError
        try:
            self._table.put_item(Item=self._item(kind, key, sort, doc, ttl_days),
                                 ConditionExpression="attribute_not_exists(pk)")
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def get(self, kind, key, sort):
        item = self._table.get_item(Key={"pk": f"{kind}#{key}", "sk": str(sort)}, ConsistentRead=True).get("Item")
        return None if item is None else json.loads(item["doc"])

    def query(self, kind, key, start=None, end=None, newest_first=False, limit=None):
        from boto3.dynamodb.conditions import Key
        cond = Key("pk").eq(f"{kind}#{key}")
        if start is not None and end is not None:
            cond = cond & Key("sk").between(str(start), str(end))
        elif start is not None:
            cond = cond & Key("sk").gte(str(start))
        elif end is not None:
            cond = cond & Key("sk").lte(str(end))
        args = {"KeyConditionExpression": cond, "ScanIndexForward": not newest_first, "ConsistentRead": True}
        out = []
        while True:
            if limit is not None:
                args["Limit"] = limit - len(out)
            page = self._table.query(**args)
            out += [(i["sk"], json.loads(i["doc"])) for i in page["Items"]]
            if "LastEvaluatedKey" not in page or (limit is not None and len(out) >= limit):
                return out
            args["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def delete(self, kind, key, sort):
        self._table.delete_item(Key={"pk": f"{kind}#{key}", "sk": str(sort)})


def create_table(table_name, resource=None):
    """Creates the table the way DynamoStore expects it. Safe to call when it already exists."""
    import boto3
    resource = resource or boto3.resource("dynamodb")
    client = resource.meta.client
    try:
        client.describe_table(TableName=table_name)
        return resource.Table(table_name)
    except client.exceptions.ResourceNotFoundException:
        pass
    table = resource.create_table(
        TableName=table_name, BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}])
    table.wait_until_exists()
    client.update_time_to_live(TableName=table_name, TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"})
    return table
