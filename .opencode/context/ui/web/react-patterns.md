<!-- Context: ui/react-patterns | Priority: low | Version: 1.1 | Updated: 2026-04-05 -->

# React Patterns & Best Practices

**Category**: development  
**Purpose**: Modern React patterns, hooks usage, and component design  
**Used by**: frontend-specialist

---

## Overview

Modern React uses functional components, hooks, and composition. This guide covers essential patterns for scalable React applications.

## Component Patterns

| Pattern | Description | When to Use |
|---------|-------------|-------------|
| **Functional Components** | Always — no class components | All components |
| **Custom Hooks** | Extract reusable logic (data fetching, form state) | Shared stateful logic |
| **Composition** | Pass components as children/props | Avoid prop drilling |
| **Compound Components** | Parent + children share implicit state | Complex UI (Tabs, Accordion) |

```jsx
// Custom hook pattern — extract stateful logic
function useUser(id) {
  const [user, setUser] = useState(null);
  useEffect(() => { fetchUser(id).then(setUser); }, [id]);
  return { user };
}
```

---

## Hooks Best Practices

- **`useEffect` deps**: Always specify all dependencies — never omit to "fix" re-renders
- **`useMemo`**: Only for expensive computations, not trivial values
- **`useCallback`**: Only when passing callbacks to memoized children
- **Derived state**: Calculate from props/state directly — avoid `useEffect` for this
- **Cleanup**: Return cleanup functions from `useEffect` for subscriptions/timers

---

## State Management

- **Start local**: `useState` first, lift up only when multiple components need it
- **Complex state**: Use `useReducer` for related/interdependent state transitions
- **Global state**: Context API for theme/auth; consider Zustand/Jotai for complex apps

---

## Performance

- **Code splitting**: `React.lazy()` + `Suspense` for route-level splitting
- **Memoization**: `React.memo()` for components that re-render often with same props
- **Virtualization**: Use `react-window`/`react-virtuoso` for lists >100 items

---

## Best Practices

- Keep components small and focused (single responsibility)
- Use TypeScript for type safety
- Colocate components, styles, and tests together
- Handle loading and error states explicitly
- Use fragments (`<>...</>`) to avoid wrapper divs
- Use stable unique IDs as `key`, never array index

## Anti-Patterns

- ❌ Prop drilling — use Context or composition
- ❌ Massive components — break into focused sub-components
- ❌ Direct state mutation — always use `setState` / `dispatch`
- ❌ Index as `key` — use stable unique identifiers
- ❌ Unnecessary `useEffect` — derive state when possible

---

## References

- [React Documentation](https://react.dev) | [React Patterns (Kent C. Dodds)](https://kentcdodds.com/blog)
