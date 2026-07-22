# @relay/sdk-ts

Generated TypeScript API client. `src/client.ts` is the hand-written transport (timeouts,
auth header, error envelope). Request/response types are generated from the API's OpenAPI
spec into `src/generated/schema.ts`:

```bash
make sdk          # dumps openapi.json from the API, then runs `npm run generate`
```

CI regenerates on every API change and fails if the committed client drifts from the spec.
