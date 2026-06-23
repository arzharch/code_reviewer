FROM python:3.11-slim

# Install git (required for cloning the PR branch) and clean up apt cache
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy the dependency specification
COPY pyproject.toml .

# Install dependencies
RUN pip install --no-cache-dir .

# Copy the rest of the application
COPY . .

# Set python path
ENV PYTHONPATH=/app

# Expose the API port
EXPOSE 8001

# Start the server
CMD ["python", "-m", "src.control_plane.server"]
