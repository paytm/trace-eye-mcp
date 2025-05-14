import json
import os
from typing import Optional, List, Dict
from datetime import datetime

import anyio
import requests
import uvicorn
from elasticsearch import Elasticsearch
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Settings, Context
from mcp.server.sse import SseServerTransport

KIBANA_URL = os.getenv("KIBANA_URL")
if KIBANA_URL:
    KIBANA_URL = KIBANA_URL.strip("/")
KIBANA_USERNAME = os.getenv("KIBANA_USERNAME")
KIBANA_PASSWORD = os.getenv("KIBANA_PASSWORD")
KIBANA_SPACE = os.getenv("KIBANA_SPACE")

KIBANA_COOKIES = None

ELASTICSEARCH_URL = os.getenv("ELASTICSEARCH_URL")
if ELASTICSEARCH_URL:
    ELASTICSEARCH_URL = ELASTICSEARCH_URL.strip("/")
ELASTICSEARCH_USERNAME = os.getenv("ELASTICSEARCH_USERNAME")
ELASTICSEARCH_PASSWORD = os.getenv("ELASTICSEARCH_PASSWORD")
ELASTICSEARCH_FINGERPRINT = os.getenv("ELASTICSEARCH_FINGERPRINT")
ELASTICSEARCH_TIMEOUT = os.getenv("ELASTICSEARCH_TIMEOUT") or 6


kibana_details_missing = not KIBANA_URL or not KIBANA_USERNAME or not KIBANA_PASSWORD or not KIBANA_SPACE
elastic_details_missing = not ELASTICSEARCH_URL
if kibana_details_missing and elastic_details_missing:
    raise ValueError("Kibana or Elasticsearch details missing")

def authenticate_elasticsearch():
    if ELASTICSEARCH_FINGERPRINT and ELASTICSEARCH_USERNAME and ELASTICSEARCH_PASSWORD:
        es = Elasticsearch(
            ELASTICSEARCH_URL,
            ssl_assert_fingerprint=ELASTICSEARCH_FINGERPRINT,
            basic_auth=(ELASTICSEARCH_USERNAME, ELASTICSEARCH_PASSWORD),
            request_timeout=ELASTICSEARCH_TIMEOUT   
        )
    elif ELASTICSEARCH_USERNAME and ELASTICSEARCH_PASSWORD:
        es = Elasticsearch(
            ELASTICSEARCH_URL,
            basic_auth=(ELASTICSEARCH_USERNAME, ELASTICSEARCH_PASSWORD),
            request_timeout=ELASTICSEARCH_TIMEOUT
        )
    elif not ELASTICSEARCH_USERNAME and not ELASTICSEARCH_PASSWORD and not ELASTICSEARCH_FINGERPRINT:
        es = Elasticsearch(ELASTICSEARCH_URL, request_timeout=ELASTICSEARCH_TIMEOUT)
    else:
        raise ValueError("Elasticsearch Initialization failed in authenticate_elasticsearch")

    return es

USE_ES = kibana_details_missing or not elastic_details_missing
if USE_ES:
    ES_CLIENT = authenticate_elasticsearch()

class EndpointConfig:
    def __init__(self, name: str, endpoint: str, log_index_path: str, es_source_fields: List[str], es_sort_field: str, es_search_query: List[str]):
        self.name = name
        self.endpoint = endpoint
        self.log_index_path = log_index_path
        self.es_source_fields = es_source_fields
        self.es_sort_field = es_sort_field
        self.es_search_query = es_search_query
        self.mcp = None


endpoint_config_data = json.load(open("endpoint_config_data.json"))

endpoint_config_map: Dict[str, EndpointConfig] = {
    key: EndpointConfig(**value) for key, value in endpoint_config_data.items()
}

fastmcp_settings: Settings = Settings(port=8090, debug=False)
for endpoint_config in endpoint_config_map.values():
    endpoint_config.mcp = FastMCP(
        endpoint_config.name,
        dependencies=["httpx", "requests"],
        **fastmcp_settings.model_dump(exclude_unset=True, exclude_defaults=True)
    )


mcp_registry = []

def register_mcp_instances(mcp_instances):
    mcp_registry.extend(mcp_instances)

register_mcp_instances([v.mcp for v in endpoint_config_map.values()])


class CustomMcpTool:
    def __init__(self):
        global mcp_registry
        self.mcp_instances = mcp_registry

    def __call__(self, func):
        for mcp_instance in self.mcp_instances:
            mcp_instance.add_tool(func)

        return func


def authenticate():
    global KIBANA_COOKIES
    if KIBANA_COOKIES is not None:
        return KIBANA_COOKIES

    response = requests.post(
        f"{KIBANA_URL}/internal/security/login",
        json={
            "providerType": "basic",
            "providerName": "basic",
            "currentURL": f"{KIBANA_URL}/login",
            "params": {"username": KIBANA_USERNAME, "password": KIBANA_PASSWORD}
        },
        headers={"kbn-xsrf": "true"},
        timeout=60
    )
    response.raise_for_status()
    cookie = response.headers.get("set-cookie")
    if ';' in cookie:
        cookie = cookie.split(';')[0]

    KIBANA_COOKIES = {cookie.split('=')[0]: cookie.split('=')[1]}
    return KIBANA_COOKIES

def search_kibana(path, query):
    cookies = authenticate()
    search_response = requests.post(
        f"{KIBANA_URL}/s/{KIBANA_SPACE}/api/console/proxy?path={path}/_search&method=GET",
        headers={
            "accept": "text/plain, */*; q=0.01",
            "accept-language": "en-IN,en;q=0.9,ja-IN;q=0.8,ja;q=0.7,en-GB;q=0.6,en-US;q=0.5",
            "cache-control": "no-cache",
            "content-type": "application/json",
            "kbn-xsrf": "kibana"
        },
        json=query,
        cookies=cookies,
        timeout=60
    )
    search_response.raise_for_status()
    return search_response.json()


def is_valid_timestamp(timestamp):
    try:
        datetime.strptime(timestamp, "%m-%dT%H:%M:%S.%f%z")
        return True
    except ValueError:
        return False

def _get_latest_log(
        endpoint_name,
        search_query: List[str],
        fetch_size: int,
        from_timestamp: str = None,
        to_timestamp: str = None
) -> Optional[str]:
    endpoint_config = endpoint_config_map[endpoint_name]
    filter_should_query = []
    for query_string in search_query:
        filter_must_query = []
        for term in query_string.split():
            filter_must_query.append({
                "multi_match": {
                    "type": "phrase",
                    "query": term,
                    "lenient": True
                }
            })
        filter_should_query.append({
            "bool": {
                "must": filter_must_query
            }
        })
    query = {
        "query": {
            "bool": {
                "filter": [
                    {
                        "bool": {
                            "should": filter_should_query
                        }
                    }
                ]
            }
        },
        "_source": endpoint_config.es_source_fields,
        "size": max(min(fetch_size, 50), 1),
        "sort": [
            {
                endpoint_config.es_sort_field: {
                    "order": "desc"
                }
            }
        ]
    }

    warning_message = ""
    if from_timestamp and not is_valid_timestamp(from_timestamp):
        from_timestamp = None
        warning_message += "Warning: Invalid from_timestamp ignored. "
    if to_timestamp and not is_valid_timestamp(to_timestamp):
        to_timestamp = None
        warning_message += "Warning: Invalid to_timestamp ignored. "

    if from_timestamp or to_timestamp:
        year = datetime.now().year
        date_range_filter = {
            "range": {
                endpoint_config.es_sort_field: {}
            }
        }
        if from_timestamp:
            date_range_filter["range"][endpoint_config.es_sort_field]["gte"] = f"{year}-{from_timestamp}"
        if to_timestamp:
            date_range_filter["range"][endpoint_config.es_sort_field]["lte"] = f"{year}-{to_timestamp}"
        query["query"]["bool"]["filter"].append(date_range_filter)
    try:
        if USE_ES:
            result = ES_CLIENT.search(index=endpoint_config.log_index_path, body=query)
        else:
            result = search_kibana(endpoint_config.log_index_path, query)
    except requests.exceptions.Timeout:
        return "Error: Request Timeout"
    except Exception as e:
        return f"Error: {e}"

    logs_source_fields = endpoint_config.es_source_fields
    hits = result.get("hits", {}).get("hits", {})
    if len(hits) == 0:
        return warning_message + "No logs found"
    else:
        if len(logs_source_fields) == 1:
            return warning_message + "\n".join([str(hit.get("_source", {}).get(logs_source_fields[0], None)) for hit in hits])
        return warning_message + "\n".join([str(hit.get("_source", {})) for hit in hits])


def get_endpoint_name(ctx: Context):
    return ctx.fastmcp.name


@CustomMcpTool()
async def search_latest_logs(
        search_query: List[str],
        ctx: Context,
        fetch_size: int = 1,
        time_range: str = None,
        from_timestamp: str = None,
        to_timestamp: str = None
) -> str:
    
    """
    This function fetches/searches log entries that match the specified search query terms, if given.
    The results are sorted as most recent to least recent by default.
    All the arguments are optional. If not provided, the default values are used.

    Args:
        search_query (List[str]): A list of search terms to filter logs.
            Terms in the same string are treated as an AND condition, while different strings are treated as an OR condition.
        fetch_size (int): The number of log entries to fetch. Defaults to 1, should be between 1 and 50.
            Keep it as low as possible. If not sure about the number of logs, use fetch_size=5.
        time_range (str): The time range for the search query like past x hours or past x days.
            format: <number><specifier>
            specifier: m for minutes, h for hours, d for days
            example: "1h" for past 1 hour, "30m" for past 30 minutes
        from_timestamp (str): The start timestamp for the search query.
            Provide this only if you want to search logs after a specific time.
            format: "MM-dd'T'HH:mm:ss.SSS+05:30" // zone is mostly IST(+05:30) if user doesn't provide
        to_timestamp (str): The end timestamp for the search query.
            Provide this only if you want to search logs before a specific time.
            format: "MM-dd'T'HH:mm:ss.SSS+05:30" // zone is mostly IST(+05:30) if user doesn't provide
        only one of time_range, (from_timestamp and/or to_timestamp) should be provided

    Returns:
        str: The latest matching log entries as a string. Returns an error message if logs cannot be fetched or no logs are found.
    """
    endpoint_name = get_endpoint_name(ctx)

    # Handle time_range parameter if provided
    if time_range and not (from_timestamp or to_timestamp):
        try:
            from datetime import timedelta, timezone

            # Parse the time range format: <number><specifier>
            # Where specifier is: m (minutes), h (hours), d (days)
            if time_range.endswith('m'):
                minutes = int(time_range[:-1])
                delta = timedelta(minutes=minutes)
            elif time_range.endswith('h'):
                hours = int(time_range[:-1])
                delta = timedelta(hours=hours)
            elif time_range.endswith('d'):
                days = int(time_range[:-1])
                delta = timedelta(days=days)
            else:
                return "Error: Invalid time_range format. Use format like '30m', '1h', '2d'"

            # Calculate from_timestamp based on current time minus the specified duration
            # Using IST timezone offset (+05:30) as mentioned in the docstring
            ist_offset = timezone(timedelta(hours=5, minutes=30))
            now = datetime.now(ist_offset)
            from_time = now - delta

            # Format the timestamp in the required format with timezone
            from_timestamp = from_time.strftime("%m-%dT%H:%M:%S.%f")[:-3] + "+05:30"

        except ValueError:
            return "Error: Invalid time_range format. Use format like '30m', '1h', '2d'"

    if not search_query:
        search_query = endpoint_config_map[endpoint_name].es_search_query
    if not fetch_size:
        fetch_size = 1
    data = _get_latest_log(endpoint_name, search_query, fetch_size, from_timestamp, to_timestamp)
    if not data:
        return "Failed to fetch logs or no logs found"
    return data


def create_handle_sse(_mcp_server, sse):
    async def handle_sse(request):
        async with sse.connect_sse(
                request.scope, request.receive, request._send
        ) as streams:
            await _mcp_server.run(
                streams[0],
                streams[1],
                _mcp_server.create_initialization_options(),
            )
    return handle_sse

async def run_sse_async(endpoint_config_map) -> None:
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route

    routes = []
    for endpoint_config in endpoint_config_map.values():
        sse = SseServerTransport(f"/{endpoint_config.endpoint}/messages/")
        routes.append(Mount(f"/{endpoint_config.endpoint}/messages/", app=sse.handle_post_message))
        _mcp_server = endpoint_config.mcp._mcp_server
        handle_sse = create_handle_sse(_mcp_server, sse)
        routes.append(Route(f"/{endpoint_config.endpoint}/sse", endpoint=handle_sse))

    settings = next(iter(endpoint_config_map.values())).mcp.settings
    starlette_app = Starlette(
        debug=settings.debug,
        routes=routes
    )

    config = uvicorn.Config(
        starlette_app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    anyio.run(run_sse_async, endpoint_config_map)