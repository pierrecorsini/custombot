<!-- Context: core/essential-patterns | Priority: critical | Version: 1.0 | Updated: 2026-02-15 -->

# Essential Patterns - Core Development Guidelines

## Quick Reference

**Core Philosophy**: Modular, Functional, Maintainable

**Critical Patterns**: Error Handling, Validation, Security, Logging, Pure Functions

**ALWAYS**: Handle errors gracefully, validate input, use env vars for secrets, write pure functions

**NEVER**: Expose sensitive info, hardcode credentials, skip input validation, mutate state

**Language-agnostic**: Apply to all programming languages

---

## Overview

This file provides essential development patterns that apply across all programming languages. For detailed standards, see:
- `standards/code-quality.md` - Modular, functional code patterns
- `standards/security-patterns.md` - Language-agnostic patterns
- `standards/test-coverage.md` - Testing standards
- `standards/documentation.md` - Documentation standards
- `standards/code-analysis.md` - Analysis framework

---

## Critical Patterns

### 1. Pure Functions
- Same input = same output, no side effects
- No mutation of external state
- Predictable and testable

### 2. Error Handling
- Catch specific errors, not generic ones
- Log errors with context, return meaningful messages
- Don't expose internal implementation details

### 3. Input Validation
- Check for null/nil/None values and data types
- Validate ranges, constraints, and sanitize user input
- Return clear validation error messages

### 4. Security
- Don't log passwords, tokens, or API keys
- Use environment variables for secrets
- Use parameterized queries (prevent SQL injection)
- Validate and escape output (prevent XSS)

### 5. Logging Levels
- **Debug**: Development-only detail
- **Info**: Important events and milestones
- **Warning**: Potential issues (non-blocking)
- **Error**: Failures and exceptions

---

## Code Structure Patterns

### Modular Design
- Single responsibility per module, clear interfaces
- Independent and composable, < 100 lines per component

### Functional Approach
- **Pure functions**: No side effects
- **Immutability**: Create new data, don't modify
- **Composition**: Build complex from simple functions

### Component Structure
```
component/
├── index.js      # Public interface
├── core.js       # Core logic (pure functions)
├── utils.js      # Helpers
└── tests/        # Tests
```

---

## Anti-Patterns to Avoid

**Code Smells**: Mutation/side effects, deep nesting (>3 levels), god modules (>200 lines), global state, hardcoded values, tight coupling

**Security Issues**: Hardcoded credentials, exposed sensitive data in logs, unvalidated input, SQL injection, XSS vulnerabilities

---

## Testing Patterns

- Write unit tests for pure functions, integration tests for components
- Test edge cases and error conditions, aim for >80% coverage
- Use descriptive test names (Arrange → Act → Assert pattern)

---

## Documentation Patterns

- Document public APIs, complex logic, non-obvious decisions, and usage examples
- Explain WHY, not just WHAT; keep it up to date

---

## Quick Checklist

Before committing: ✅ Pure functions ✅ Input validation ✅ Error handling ✅ No hardcoded secrets ✅ Tests passing ✅ Documentation updated ✅ No security vulnerabilities ✅ Modular and maintainable

---

## Additional Resources

- `standards/code-quality.md` - Comprehensive code standards
- `standards/security-patterns.md` - Detailed pattern catalog
- `standards/test-coverage.md` - Testing best practices
- `workflows/code-review.md` - Code review process
