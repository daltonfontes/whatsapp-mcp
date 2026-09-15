FROM golang:1.26 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY *.go panel.html ./
RUN CGO_ENABLED=1 go build -o /whatsapp-bridge .

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app/whatsapp-bridge
COPY --from=build /whatsapp-bridge .
EXPOSE 8080
CMD ["./whatsapp-bridge"]
