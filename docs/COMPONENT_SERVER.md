# Distributed Component Server/Client System

This feature allows Predbat components to run on a separate server, useful for Kubernetes deployments or load distribution.

## Architecture

- **Component Server**: Standalone server that hosts remote component instances (e.g., OctopusAPI)
- **Component Client**: Proxy in main Predbat that forwards operations to the server via REST
- **Callback Server**: HTTP server in Predbat that handles base API callbacks from remote components

## Security Warning

⚠️ **WARNING**: This system uses pickle for serialization. Only use in trusted networks (e.g., Kubernetes cluster) as pickle can execute arbitrary code. Do not expose to untrusted networks or the internet.

## Configuration

Add this section to your `apps.yaml` under the Predbat app configuration:

```yaml
predbat:
  module: predbat
  class: PredBat
  # ... other configuration ...

  # Component server configuration (infrastructure settings)
  component_server:
    # URL of the component server
    url: "http://componentserver:5053"

    # List of components to run remotely (component names from components.py)
    components:
      - octopus

    # Callback URL where this Predbat instance can be reached by the server
    # If not specified, server will use X-Forwarded-For or remote IP
    callback_url: "http://predbat:5054"

    # Port for callback server (default: 5054)
    callback_port: 5054

    # How often client pings server (seconds, default: 300)
    poll_interval: 300

    # Component timeout - server stops component after this many seconds of no pings (default: 1800)
    timeout: 1800
```

### Configuration Keys

| Key | Type | Required | Default | Description |
|-----|------|----------|---------|-------------|
| `url` | string | Yes | - | HTTP URL of component server |
| `components` | list | Yes | - | Component names to run remotely |
| `callback_url` | string | No | Auto-detect | URL where Predbat callback server is accessible |
| `callback_port` | int | No | 5054 | Port for Predbat callback server |
| `poll_interval` | int | No | 300 | Ping interval in seconds |
| `timeout` | int | No | 1800 | Component inactivity timeout in seconds |

## Running the Component Server

```bash
cd apps/predbat
./run_component_server.py --host 0.0.0.0 --port 5053 --timeout 1800 --log-level INFO
```

### Command Line Options

- `--host`: Server bind address (default: 0.0.0.0)
- `--port`: Server port (default: 5053)
- `--timeout`: Component inactivity timeout in seconds (default: 1800)
- `--log-level`: Logging level - DEBUG, INFO, WARNING, ERROR (default: INFO)
- `--log-file`: Optional log file path

## How It Works

1. **Startup**:
   - Predbat starts callback server on configured port
   - For components in `component_server_components` list, creates `ComponentClient` instead of actual component
   - Client connects to component server and requests component start

2. **Operation**:
   - Client forwards all method calls (run, events, etc.) to server via HTTP POST
   - Server invokes methods on actual component instance
   - When component needs base API (get_arg, get_state_wrapper, etc.), server calls back to Predbat
   - Results are pickled and returned to client

3. **Health Monitoring**:
   - Client sends ping every `component_server_poll_interval` seconds
   - Server auto-stops components that haven't pinged in `timeout` seconds
   - Client auto-restarts components if server reports "Component not found"

4. **Shutdown**:
   - Client sends stop request to server
   - Server gracefully stops component and cleans up resources

## Supported Components

Currently registered for remote execution:

- OctopusAPI

To add more components, edit `COMPONENT_CLASSES` in `run_component_server.py`.

## Kubernetes Deployment Example

### Component Server Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: predbat-component-server
spec:
  replicas: 1
  selector:
    matchLabels:
      app: predbat-component-server
  template:
    metadata:
      labels:
        app: predbat-component-server
    spec:
      containers:
      - name: server
        image: predbat:latest
        command: ["python3", "/app/apps/predbat/run_component_server.py"]
        args: ["--host", "0.0.0.0", "--port", "5053", "--timeout", "1800"]
        ports:
        - containerPort: 5053
---
apiVersion: v1
kind: Service
metadata:
  name: componentserver
spec:
  selector:
    app: predbat-component-server
  ports:
  - port: 5053
    targetPort: 5053
```

### Predbat Deployment

Ensure Predbat has a Service exposing port 5054 for callbacks:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: predbat
spec:
  selector:
    app: predbat
  ports:
  - name: web
    port: 5052
    targetPort: 5052
  - name: callback
    port: 5054
    targetPort: 5054
```

## Troubleshooting

### Component won't start

- Check callback URL is reachable from component server
- Verify component server URL is correct
- Check logs for connection errors

### Component keeps restarting

- Check ping interval isn't too long relative to timeout
- Verify network connectivity between server and client
- Review server logs for timeout messages

### Performance issues

- Reduce number of remote components (network overhead per call)
- Increase timeout values if network is slow
- Check server resources (CPU, memory)

## Testing

The feature is optional - Predbat works normally when `component_server_components` is empty or `component_server_url` is not set.

To test:

1. Start component server manually
2. Configure Predbat with server URL and component list
3. Restart Predbat
4. Check logs for "Creating remote component client" messages
5. Verify component functionality (e.g., Octopus rates updating)
