# OpenTripPlanner - Minneapolis Metro Transit Deployment
# Multi-stage build for optimized image size

# Build stage
FROM eclipse-temurin:21-jdk AS builder

WORKDIR /build

# Install Maven
RUN apt-get update && \
    apt-get install -y --no-install-recommends maven && \
    rm -rf /var/lib/apt/lists/*

# Copy OTP source code
COPY OpentripPlanner .

# Build OTP (skip tests for faster build)
RUN mvn clean package -DskipTests

# Runtime stage
FROM eclipse-temurin:21-jre

LABEL maintainer="Minneapolis OTP"
LABEL description="OpenTripPlanner configured for Minneapolis-St. Paul Metro Transit"

# Install utilities
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Copy built JAR from builder stage
COPY --from=builder /build/otp-shaded/target/otp-shaded-*.jar /app/otp.jar

# Copy configuration files
COPY config/ /etc/otp/

# Create data volume
VOLUME /var/opentripplanner

# Expose OTP port
EXPOSE 8080

# Set memory limit (override with JAVA_OPTS env var)
ENV JAVA_OPTS="-Xmx2G"

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD curl -f http://localhost:8080/otp/routers/default || exit 1

# Run OTP
ENTRYPOINT java $JAVA_OPTS -jar /app/otp.jar --load /var/opentripplanner

# Default command (can be overridden)
CMD []
