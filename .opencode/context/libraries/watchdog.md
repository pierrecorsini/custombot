### Monitor File System Changes with Observer

Source: https://context7.com/gorakhargosh/watchdog/llms.txt

Demonstrates the basic usage of the Observer class to monitor a directory. It uses a custom FileSystemEventHandler to print event details when file system changes occur.

```python
import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

class MyHandler(FileSystemEventHandler):
    def on_any_event(self, event: FileSystemEvent) -> None:
        print(f"Event type: {event.event_type}, Path: {event.src_path}")

# Create observer and handler
observer = Observer()
handler = MyHandler()

# Schedule watching a directory (recursive=True monitors subdirectories)
watch = observer.schedule(handler, path="/path/to/watch", recursive=True)

# Start monitoring
observer.start()
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    observer.stop()
observer.join()
```

--------------------------------

### Monitor Directory Recursively with Watchdog

Source: https://github.com/gorakhargosh/watchdog/blob/master/docs/source/quickstart.md

This example monitors the current directory recursively for file system changes and prints events to the console. It requires importing necessary classes from `watchdog.events` and `watchdog.observers`. The observer is started and kept alive with a `while True` loop, and stopped gracefully in a `finally` block.

```python
import time

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer


class MyEventHandler(FileSystemEventHandler):
    def on_any_event(self, event: FileSystemEvent) -> None:
        print(event)


event_handler = MyEventHandler()
observer = Observer()
observer.schedule(event_handler, ".", recursive=True)
observer.start()
try:
    while True:
        time.sleep(1)
finally:
    observer.stop()
    observer.join()
```

--------------------------------

### Implement Comprehensive FileSystemEventHandler

Source: https://context7.com/gorakhargosh/watchdog/llms.txt

Provides an example of extending the FileSystemEventHandler base class to handle specific file and directory events like creation, deletion, modification, and movement.

```python
from watchdog.observers import Observer
from watchdog.events import (
    FileSystemEventHandler,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    DirCreatedEvent,
    DirDeletedEvent,
    DirMovedEvent,
)

class ComprehensiveHandler(FileSystemEventHandler):
    def on_created(self, event: FileCreatedEvent | DirCreatedEvent) -> None:
        kind = "Directory" if event.is_directory else "File"
        print(f"{kind} created: {event.src_path}")

    def on_deleted(self, event: FileDeletedEvent | DirDeletedEvent) -> None:
        kind = "Directory" if event.is_directory else "File"
        print(f"{kind} deleted: {event.src_path}")

    def on_modified(self, event: FileModifiedEvent) -> None:
        if not event.is_directory:
            print(f"File modified: {event.src_path}")

    def on_moved(self, event: FileMovedEvent | DirMovedEvent) -> None:
        kind = "Directory" if event.is_directory else "File"
        print(f"{kind} moved: {event.src_path} -> {event.dest_path}")

    def on_closed(self, event) -> None:
        print(f"File closed (after write): {event.src_path}")

    def on_opened(self, event) -> None:
        print(f"File opened: {event.src_path}")

# Usage
handler = ComprehensiveHandler()
observer = Observer()
observer.schedule(handler, "/path/to/watch", recursive=True)
observer.start()
```

--------------------------------

### File System Event Logging with LoggingEventHandler

Source: https://context7.com/gorakhargosh/watchdog/llms.txt

Uses the built-in LoggingEventHandler to log all file system events to Python's logging system. This is useful for debugging and creating audit trails. Events are logged to both a file ('file_changes.log') and the console.

```python
import logging
import time
from watchdog.observers import Observer
from watchdog.events import LoggingEventHandler

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('file_changes.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger('file_watcher')
handler = LoggingEventHandler(logger=logger)

observer = Observer()
observer.schedule(handler, path=".", recursive=True)

with observer:
    print("Logging file system events...")
    while True:
        time.sleep(1)
```

--------------------------------

### LoggingEventHandler

Source: https://context7.com/gorakhargosh/watchdog/llms.txt

Logs all file system events to Python's logging system.

```APIDOC
## LoggingEventHandler

A built-in event handler that logs all file system events to Python's logging system, useful for debugging and audit trails.

### Description
This handler captures all file system events and logs them using the standard Python logging module.

### Method
N/A (Class definition)

### Endpoint
N/A (Class definition)

### Parameters
#### Path Parameters
N/A

#### Query Parameters
N/A

#### Request Body
N/A

### Request Example
```python
import logging
from watchdog.observers import Observer
from watchdog.events import LoggingEventHandler

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('file_changes.log'), logging.StreamHandler()]
)

logger = logging.getLogger('file_watcher')
handler = LoggingEventHandler(logger=logger)
observer = Observer()
observer.schedule(handler, path=".", recursive=True)
observer.start()

# Keep the observer running
while True:
    time.sleep(1)
```

### Response
#### Success Response (200)
N/A (This is a class definition for event handling)

#### Response Example
N/A
```