### Configure TracerProvider and create spans

Source: https://context7.com/open-telemetry/opentelemetry-python/llms.txt

Installs the SDK's TracerProvider and configures it with a resource and console exporter. Spans are created using a context manager for automatic ending and active span management.

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.resources import Resource

# Configure SDK TracerProvider with a resource and console exporter
resource = Resource.create({"service.name": "my-service", "service.version": "1.0.0"})
provider = TracerProvider(resource=resource)
provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)

tracer = trace.get_tracer("my.instrumentation.library", "1.0.0")

# Context manager: auto-sets as current span, auto-ends on exit
with tracer.start_as_current_span(
    "parent-operation",
    kind=trace.SpanKind.SERVER,
    attributes={"http.method": "GET", "http.url": "https://example.com/api"},
) as parent:
    parent.add_event("cache-miss", attributes={"cache.key": "user:42"})

    with tracer.start_as_current_span("child-db-query") as child:
        child.set_attribute("db.system", "postgresql")
        child.set_attribute("db.statement", "SELECT * FROM users WHERE id = $1")
        # simulate work
        result = {"id": 42, "name": "Alice"}

    parent.set_status(trace.StatusCode.OK)

# Expected output: JSON span data on stdout showing trace_id, span_id, parent_span_id,
# name, kind, attributes, events, status, start/end timestamps

```

--------------------------------

### trace.get_tracer() / TracerProvider Configuration

Source: https://context7.com/open-telemetry/opentelemetry-python/llms.txt

Demonstrates how to configure the SDK's TracerProvider with a resource and exporter, and then retrieve a tracer to create spans using context managers.

```APIDOC
## trace.get_tracer() / TracerProvider Configuration

### Description
This section shows how to set up the global `TracerProvider` with a resource and an exporter, and then obtain a `Tracer` instance. It illustrates creating spans using `start_as_current_span` which acts as a context manager, automatically handling span start and end times, and setting the span as the current one in the context.

### Method
`trace.get_tracer(name, version=None)`
`trace.set_tracer_provider(provider)`
`TracerProvider(resource=None)`
`provider.add_span_processor(processor)`
`tracer.start_as_current_span(name, kind=None, attributes=None, links=None, start_time=None)`

### Parameters
#### `trace.get_tracer` Parameters
- **name** (string) - Required - The name of the instrumentation library.
- **version** (string) - Optional - The version of the instrumentation library.

#### `TracerProvider` Parameters
- **resource** (`Resource`) - Optional - The resource associated with the telemetry data.

#### `provider.add_span_processor` Parameters
- **processor** (`SpanProcessor`) - Required - The span processor to add to the pipeline.

#### `tracer.start_as_current_span` Parameters
- **name** (string) - Required - The name of the span.
- **kind** (`SpanKind`) - Optional - The kind of the span (e.g., SERVER, CLIENT, INTERNAL).
- **attributes** (dict) - Optional - Key-value pairs to associate with the span.
- **links** (list of `Link`) - Optional - Links to other spans.
- **start_time** (`datetime`) - Optional - The explicit start time of the span.

### Request Example
```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.resources import Resource

# Configure SDK TracerProvider with a resource and console exporter
resource = Resource.create({"service.name": "my-service", "service.version": "1.0.0"})
provider = TracerProvider(resource=resource)
provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)

tracer = trace.get_tracer("my.instrumentation.library", "1.0.0")

# Context manager: auto-sets as current span, auto-ends on exit
with tracer.start_as_current_span(
    "parent-operation",
    kind=trace.SpanKind.SERVER,
    attributes={"http.method": "GET", "http.url": "https://example.com/api"},
) as parent:
    parent.add_event("cache-miss", attributes={"cache.key": "user:42"})

    with tracer.start_as_current_span("child-db-query") as child:
        child.set_attribute("db.system", "postgresql")
        child.set_attribute("db.statement", "SELECT * FROM users WHERE id = $1")
        # simulate work
        result = {"id": 42, "name": "Alice"}

    parent.set_status(trace.StatusCode.OK)
```

### Response
#### Success Response
Console output with JSON span data showing trace_id, span_id, parent_span_id, name, kind, attributes, events, status, and start/end timestamps.
```

--------------------------------

### opentelemetry.sdk.trace

Source: https://github.com/open-telemetry/opentelemetry-python/blob/main/docs/sdk/trace.rst

The opentelemetry.sdk.trace package provides the core implementation for distributed tracing within the OpenTelemetry Python SDK. It includes essential components for managing spans, generating trace IDs, and controlling sampling decisions.

```APIDOC
## opentelemetry.sdk.trace

### Description
Provides the core implementation for distributed tracing, including span management, trace ID generation, and sampling.

### Submodules
- **trace.export**: Handles exporting trace data to backends.
- **trace.id_generator**: Manages the generation of unique trace and span IDs.
- **trace.sampling**: Implements strategies for deciding whether to sample traces.
- **util.instrumentation**: Utility functions for instrumentation.

### Members
This module exposes various classes and functions for trace management, including but not limited to:

- **TracerProvider**: The entry point for creating tracers and managing the trace lifecycle.
- **Span**: Represents a single operation within a trace.
- **Sampler**: Interface for sampling decisions.
- **SpanContext**: Contains the unique identifiers for a span.

### Inheritance
This module inherits from `opentelemetry.sdk.trace`.
```

--------------------------------

### Configure OTLP gRPC Metric Exporter

Source: https://context7.com/open-telemetry/opentelemetry-python/llms.txt

Export metrics to an OpenTelemetry Collector via gRPC. Use PeriodicExportingMetricReader for scheduled exports.

```python
# --- OTLP Metrics (gRPC) ---
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

metric_exporter = OTLPMetricExporter(endpoint="http://otel-collector:4317", insecure=True)
reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=10_000)
provider = MeterProvider(metric_readers=[reader])
```

### OpenTelemetry Python SDK

Source: https://github.com/open-telemetry/opentelemetry-python/blob/main/docs/sdk/index.rst

The OpenTelemetry Python SDK provides the reference implementation of the OpenTelemetry Python API. It includes concrete classes for managing and exporting traces, metrics, and logs, such as TracerProvider, MeterProvider, span processors, metric readers, and exporters. The SDK is responsible for sampling, batching, and delivering telemetry data to backends.