/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Consume workspace packages as TypeScript source.
  transpilePackages: ["@relay/shared", "@relay/sdk-ts"],
  // The agent app is CSR behind auth; marketing is statically generated. No hot SSR paths.
  typedRoutes: true,
  webpack: (config) => {
    // `@relay/shared` is authored as ESM TS with `.js`-extensioned relative imports (NodeNext
    // style). When webpack pulls that package's *runtime* graph, teach it to resolve `.js`
    // specifiers to the `.ts`/`.tsx` sources.
    config.resolve.extensionAlias = {
      ...(config.resolve.extensionAlias ?? {}),
      ".js": [".ts", ".tsx", ".js", ".jsx"],
    };
    return config;
  },
};

export default nextConfig;
