/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Consume workspace packages as TypeScript source.
  transpilePackages: ["@relay/shared", "@relay/sdk-ts"],
  // The agent app is CSR behind auth; marketing is statically generated. No hot SSR paths.
  typedRoutes: true,
};

export default nextConfig;
