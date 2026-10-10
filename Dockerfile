FROM debian:bookworm-slim
WORKDIR /app

# Hermetic Turnkey Container: directly bundles the precompiled stripped binary
COPY bin/silicium_server /app/silicium_server
RUN chmod +x /app/silicium_server

EXPOSE 3000
ENTRYPOINT ["/app/silicium_server"]
