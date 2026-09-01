FROM python:3.11-slim

# Install git (required for cloning the PR branch) and clean up apt cache
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy the application (setuptools needs the packages and the README declared
# in pyproject.toml present at install time)
COPY . .

# Install the app plus the static-analysis binaries the agent shells out to
RUN pip install --no-cache-dir ".[analysis]"

# Set python path
ENV PYTHONPATH=/app

# Expose the API port
EXPOSE 8001

# Start the server
CMD ["python", "-m", "src.control_plane.server"]
