IMAGE := split-video
PORT  ?= 8765
DIR   ?= $(PWD)

.PHONY: help build dev test fetch-model

.DEFAULT_GOAL := help

help: ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

build: ## Build the Docker image
	docker build -t $(IMAGE) .

fetch-model: ## Download the YAMNet model for local (non-Docker) dev; the Docker build fetches it itself
	uv run --with kagglehub python scripts/fetch_yamnet_model.py

dev: build ## Build and run the visual editor in Docker (override with DIR=./videos PORT=9000)
	docker run --rm \
		--name split-video-editor \
		-p $(PORT):8765 \
		-v "$(DIR)":/data \
		--user "$$(id -u):$$(id -g)" \
		$(IMAGE) \
		edit /data --host 0.0.0.0 --no-browser

test: ## Run the test suite locally via uv
	uv run pytest
