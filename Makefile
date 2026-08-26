IMAGE := split-video
PORT  ?= 8765
DIR   ?= $(PWD)

.PHONY: dev build test

build:
	docker build -t $(IMAGE) .

dev: build
	docker run --rm \
		--name split-video-editor \
		-p $(PORT):8765 \
		-v "$(DIR)":/data \
		--user "$$(id -u):$$(id -g)" \
		$(IMAGE) \
		edit /data --host 0.0.0.0 --no-browser

test:
	uv run pytest
