rebuild:
	@docker compose up -d --build

hard_rebuild:
	@docker compose down --remove-orphans && docker compose up -d --build

start:
	@docker compose up -d

stop:
	@docker compose stop

restart: stop start

logs:
	@docker compose logs -f app

connect:
	@docker exec -it meshlink_app bash
