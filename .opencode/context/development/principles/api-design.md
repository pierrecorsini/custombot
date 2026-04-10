<!-- Context: development/api-design | Priority: low | Version: 1.1 | Updated: 2026-04-05 -->

# API Design Patterns

**Category**: development  
**Purpose**: REST API design principles and best practices  
**Used by**: opencoder

---

## Overview

Principles for designing robust, consistent REST APIs: resource-based URLs, proper HTTP methods, standard status codes, and predictable response formats.

## REST API Key Concepts

### Resource URLs

Use nouns, not verbs. Express actions via HTTP methods.

```
GET    /users        # List users
POST   /users        # Create user
GET    /users/123    # Get user
PATCH  /users/123    # Update user
DELETE /users/123    # Delete user
```

### HTTP Methods

| Method | Purpose | Idempotent |
|--------|---------|------------|
| `GET` | Retrieve resources | Yes |
| `POST` | Create new resources | No |
| `PUT` | Replace entire resource | Yes |
| `PATCH` | Partial update | Yes |
| `DELETE` | Remove resource | Yes |

### Status Codes

| Code | Meaning | Use When |
|------|---------|----------|
| `200` | OK | Successful GET, PUT, PATCH |
| `201` | Created | Successful POST |
| `204` | No Content | Successful DELETE |
| `400` | Bad Request | Invalid input |
| `401` | Unauthorized | Missing/invalid auth |
| `403` | Forbidden | Authenticated but not authorized |
| `404` | Not Found | Resource doesn't exist |
| `409` | Conflict | Duplicate or conflict |
| `422` | Unprocessable | Validation errors |
| `500` | Server Error | Unexpected failure |

### Response Format

Standardize on a consistent structure:

```json
{ "data": { "id": "123", "name": "John" }, "meta": { "timestamp": "..." } }
{ "error": { "code": "VALIDATION_ERROR", "message": "Invalid input" } }
{ "data": [...], "meta": { "total": 100, "page": 1 }, "links": { "next": "?page=2" } }
```

### Query Operations

- **Filter**: `GET /users?status=active&role=admin`
- **Sort**: `GET /users?sort=createdAt:desc`
- **Paginate**: `GET /users?page=2&pageSize=20`
- **Search**: `GET /users?q=john`

### Nesting

Prefer shallow routes: `GET /users/123/posts` or `GET /posts?userId=123`. Avoid deep nesting (`/users/123/posts/456/comments/789`).

---

## Versioning

- **URL**: `/v1/users`, `/v2/users` — clear but URL changes
- **Header**: `Accept: application/vnd.myapi.v2+json` — clean URLs, harder to test
- **Deprecation**: Use `Deprecation` + `Sunset` headers, provide migration guide

---

## Best Practices

- Use HTTPS everywhere; encrypt all traffic
- Implement rate limiting to prevent abuse
- Validate all inputs — never trust client data
- Return consistent error format with meaningful codes
- Document with OpenAPI/Swagger
- Always paginate collection endpoints
- Use ETags and `Cache-Control` for caching

## Anti-Patterns

- ❌ Verbs in URLs (`/getUsers`, `/createUser`)
- ❌ Returning too much data — support field selection
- ❌ Inconsistent naming — pick camelCase or snake_case
- ❌ Missing pagination on collections
- ❌ Leaking implementation details in errors

---

## References

- [RESTful API Design](https://restfulapi.net/) | [OpenAPI Specification](https://swagger.io/specification/)
- [API Design Patterns (JJ Geewax)](https://www.oreilly.com/library/view/api-design-patterns/9781617295850/)
- [GraphQL Best Practices](https://graphql.org/learn/best-practices/) — for GraphQL projects
