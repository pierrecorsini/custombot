<!-- Context: development/typing-protocol-patterns | Priority: medium | Version: 1.0 | Updated: 2026-03-24 -->

# Python Protocol Advanced Patterns

**Purpose**: Advanced Protocol patterns for complex type scenarios.

---

## Self-Types (Cloneable Pattern)

```python
from typing import Protocol, TypeVar

T = TypeVar('T')

class Cloneable(Protocol[T]):
    def clone(self: T) -> T: ...

class DataPoint:
    def clone(self) -> 'DataPoint':
        return DataPoint(self.value)
```

---

## Recursive Protocols (Tree Structures)

```python
class TreeNode(Protocol):
    def children(self) -> Iterable['TreeNode']: ...
    def value(self) -> int: ...
```

---

## Factory Pattern

```python
class Factory(Protocol[T]):
    @classmethod
    def create(cls, **kwargs) -> T: ...
    @classmethod
    def from_string(cls, s: str) -> T: ...

def make_item(factory: type[Factory[T]], config: str) -> T:
    return factory.from_string(config)
```

---

## Async Context Manager

```python
class AsyncContextManager(Protocol[T]):
    async def __aenter__(self) -> T: ...
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool | None: ...
```

---

## Protocol Intersection

```python
class Serializable(Protocol):
    def to_json(self) -> str: ...

class Comparable(Protocol):
    def __lt__(self, other) -> bool: ...

# Must satisfy both
class SortedCacheable(Serializable, Hashable, Comparable, Protocol):
    pass
```

---

## Bidirectional Channel Pattern

```python
T_send = TypeVar('T_send', contravariant=True)
T_recv = TypeVar('T_recv', covariant=True)

class AsyncChannel(Protocol[T_send, T_recv]):
    async def send(self, message: T_send) -> None: ...
    async def receive(self) -> T_recv: ...
    
    async def __aiter__(self) -> AsyncIterator[T_recv]:
        while True:
            yield await self.receive()
```

---

## Skill System Pattern (Project-Relevant)

```python
@dataclass
class SkillContext:
    user_id: str
    session_id: str
    metadata: dict[str, Any]

class Skill(Protocol[T_input, T_output]):
    @property
    def name(self) -> str: ...
    @property
    def version(self) -> str: ...
    
    async def validate_input(self, data: T_input) -> bool: ...
    async def execute(self, input_data: T_input, context: SkillContext) -> T_output: ...

# Registry
class SkillRegistry:
    def __init__(self):
        self._skills: dict[str, Skill[Any, Any]] = {}
    
    def register(self, skill: Skill[Any, Any]) -> None:
        self._skills[skill.name] = skill
    
    async def execute(self, name: str, input_data: Any, ctx: SkillContext) -> Any:
        skill = self._skills.get(name)
        if not skill or not await skill.validate_input(input_data):
            raise ValueError(f"Invalid: {name}")
        return await skill.execute(input_data, ctx)
```

---

## Type Narrowing with isinstance()

```python
@runtime_checkable
class CanRead(Protocol):
    async def read(self) -> bytes: ...

@runtime_checkable
class CanWrite(Protocol):
    async def write(self, data: bytes) -> None: ...

async def process(resource: Union[CanRead, CanWrite]) -> None:
    if isinstance(resource, CanRead):
        data = await resource.read()  # Type narrowed!
    elif isinstance(resource, CanWrite):
        await resource.write(b"hello")
```

---

## Best Practices

1. **Keep protocols small** - Single responsibility
2. **Use `...` for method bodies** - Convention for stub methods
3. **Prefer implicit implementation** - Let structural typing work
4. **Use `@runtime_checkable` sparingly** - Only when needed
5. **Document intent** - Add docstrings explaining contract

---

**Reference**: https://peps.python.org/pep-0544/
**Related**: typing-protocol.md, src/protocols.py
