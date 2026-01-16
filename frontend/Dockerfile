# Dockerfile for otp-react-redux frontend
FROM node:20-alpine

WORKDIR /app

# Install git (needed to clone the repository)
RUN apk add --no-cache git

# Clone otp-react-redux (using rightwaytrey's fork with custom changes)
RUN git clone https://github.com/rightwaytrey/otp-react-redux.git .

# Install dependencies
RUN yarn install

# Copy the custom configuration
COPY port-config.yml /app/port-config.yml

# Expose frontend port
EXPOSE 9967

# Set environment variable for config
ENV YAML_CONFIG=/app/port-config.yml

# Start the development server
CMD ["yarn", "start"]
