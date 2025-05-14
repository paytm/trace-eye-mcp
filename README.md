

# TraceEye Project Setup and Usage

This document outlines the steps to set up and run the project, configure necessary services, and integrate with Cursor MCP.

## Prerequisites

1.  **Set ElasticSearch Credentials**:
    Create or update a `.env` file in the root of the project with your ElasticSearch credentials:

    ```env
    ELASTICSEARCH_URL=<your_elasticsearch_url>
    ELASTICSEARCH_USERNAME=<your_elasticsearch_username>
    ELASTICSEARCH_PASSWORD=<your_elasticsearch_password>
    ELASTICSEARCH_FINGERPRINT=<your_elasticsearch_fingerprint>
    ```

## Configuration

1.  **Add Service to Endpoint Configuration**:
    Modify the `endpoint_config_data.json` file to include your service details. Replace `<service_name>` with the actual name of your service.

    ```json
    {
        "<service_name>": {
            "name": "<service_name>",
            "endpoint": "<service_name>",
            "log_index_path": "fse_backend_access_app*,fse_access_nginx*",
            "es_source_fields": ["message"],
            "es_sort_field": "@timestamp",
            "es_search_query": ["error exception"]
        }
    }
    ```

## Installation and Running the Application

1.  **Create and Activate Virtual Environment**:
    It is recommended to use a virtual environment. Replace `<env_name>` with your preferred environment name.

    ```bash
    python3.13 -m venv <env_name>
    source <env_name>/bin/activate
    ```

2.  **Install Requirements**:
    Install the necessary Python packages using the `requirements.txt` file.

    ```bash
    pip install -r requirements.txt
    ```

3.  **Run the Application**:
    Execute the `main.py` script to start the application.

    ```bash
    python main.py
    ```
    This will expose an MCP endpoint.

## MCP Endpoint

The application will expose an MCP endpoint at the following URL structure:

`http://localhost:8090/<service_name>/sse`

Replace `<service_name>` with the name you configured in `endpoint_config_data.json`.

## Cursor MCP Integration

1.  **Add URL to Cursor MCP Configuration**:
    Update your Cursor `mcp.json` file to include the new MCP server. Replace `ServiceNameProdLogs` with a descriptive name for your service and `<service_name>` with your actual service name.

    ```json
    {
        "mcpServers": {
            "ServiceNameProdLogs":{
               "url": "http://localhost:8090/<service_name>/sse"
            }
        }
    }
    ```

## Expected Outcome

After completing these steps, the `search_latest_logs` tool should be exposed and available in Cursor MCP tools.
