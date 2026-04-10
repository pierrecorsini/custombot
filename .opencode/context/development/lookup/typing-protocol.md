<!-- Context: development/typing-protocol | Priority: high | Version: 1.0 | Updated: 2026-03-24 -->

# Python typing.Protocol Quick Reference

**Core Idea**: Structural subtyping (static duck typing). Classes satisfy protocols by having matching methods/attributes—no explicit inheritance needed.

---

## Basic Definition

```python
from typing import Protocol, runtime_checkable

class SupportsClose(Protocol):
    def close(self) -> None: ...

# Any class with close() implicitly satisfies this protocol
class Resource:
    def close(self) -> None:
        self.file.close()

def close_all(things: list[SupportsClose]) -> None:
    for t in things:
        t.close()

close_all([Resource(), open('foo.txt')])  # OK!
```

---

## Key Patterns

### Attributes & Properties
```python
class Template(Protocol):
    name: str              # Instance variable
    count: ClassVar[int]   # Class variable
    
    @property
    def id(self) -> int: ...  # Read-only property
```

### Runtime Checking
```python
@runtime_checkable
class Checkable(Protocol):
    def method(self) -> int: ...

isinstance(obj, Checkable)  # Works! (only checks existence, not signature)
```

### Generic Protocols
```python
T = TypeVar('T')

class Container(Protocol[T]):
    value: T
    def process(self, item: T) -> T: ...
```

### Async Protocols
```python
class AsyncChannel(Protocol):
    async def send(self, data: bytes) -> None: ...
    async def receive(self) -> bytes: ...
```

### Extending Protocols
```python
class Extended(BaseProto, Protocol):  # MUST include Protocol!
    def extra(self) -> None: ...
```

---

## Common Pitfalls

| Pitfall | Wrong | Correct |
|---------|-------|---------|
| Forgetting Protocol in bases | `class Ext(Base): ...` | `class Ext(Base, Protocol): ...` |
| Runtime check signature | Trusts `isinstance` | Use static type checkers |
| Data attrs + issubclass | `issubclass(Cls, Proto)` | Only `isinstance()` works |
| Extending non-Protocol | `class P(RegularClass, Protocol)` | Can't extend regular classes |

---

## Protocol vs ABC

| Use Protocol When | Use ABC When |
|-------------------|--------------|
| Third-party types | Shared implementation |
| Duck-typing | Constructor behavior |
| Multiple implementations | Single hierarchy |
| Minimal coupling | Need `super()` calls |

---

## Project Pattern: Channel Protocol

```python
# Used in src/protocols.py
class Channel(Protocol):
    async def send(self, message: dict) -> None: ...
    async def receive(self) -> dict: ...
    async def close(self) -> None: ...
```

---

**Reference**: https://docs.python.org/3/library/typing.html#typing.Protocol
**Related**: src/protocols.py, src/type_guards.py
