# Pointy — Planning Poker

Realtime Planning Poker dla zespołu: uczestnicy wchodzą przez link, głosują
prywatnie, a host jednocześnie odsłania karty i statystyki. Zmiany docierają do
wszystkich przez WebSocket, bez odświeżania strony.

Stan pokojów trzymany jest w Redisie, więc aplikacja działa w wielu procesach i
instancjach naraz, przeżywa restart oraz sama sprząta nieużywane pokoje.
Wdrożenie na VPS to jeden `docker compose up -d` z HTTPS od Let's Encrypt.

---

## Szybki start (lokalnie, bez Dockera)

Wymagany Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python run.py
```

Jeśli systemowy Python nie ma `ensurepip` (minimalne instalacje Ubuntu):

```bash
python3 -m venv --without-pip .venv
pip --python .venv install -e '.[test]'
.venv/bin/python run.py
```

Otwórz <http://127.0.0.1:8000>. Aby zasymulować kilka osób, otwórz link pokoju w
innych oknach lub kartach prywatnych — sesja jest per karta (`sessionStorage`).

Bez zmiennej `REDIS_URL` aplikacja używa magazynu w pamięci procesu: wygodne przy
pracy nad UI, ale **tylko do dewelopmentu** (jeden proces, stan ginie przy
restarcie). Aplikacja wypisuje wtedy ostrzeżenie przy starcie.

### Lokalnie z Redisem (tak jak na produkcji)

```bash
docker run -d --rm -p 6379:6379 --name pointy-redis redis:7-alpine
REDIS_URL=redis://127.0.0.1:6379/0 .venv/bin/python run.py
```

`GET /healthz` pokaże wtedy `{"status":"ok","store":"redis"}`.

---

## Uruchomienie kontenerowe

```bash
cp .env.example .env
${EDITOR:-nano} .env          # APP_DOMAIN, ACME_EMAIL, REDIS_PASSWORD
docker compose up -d
```

Stack to trzy usługi:

| usługa  | rola |
| ------- | ---- |
| `caddy` | reverse proxy, terminacja TLS, automatyczne certyfikaty, HTTP/3 |
| `app`   | FastAPI + uvicorn (`APP_WORKERS` procesów), tylko w sieci wewnętrznej |
| `redis` | współdzielony stan pokojów i pub/sub, wolumen `redis-data` |

Do testu na własnej maszynie wystarczy `APP_DOMAIN=http://localhost` — Caddy
wystawi wtedy zwykłe HTTP na porcie 80 i nie będzie próbował wystawiać
certyfikatu. Z prawdziwą domeną (`APP_DOMAIN=poker.example.com`) HTTPS włącza się
samo przy pierwszym starcie.

Sekret Redisa wygeneruj, nie kopiuj z przykładu:

```bash
openssl rand -base64 32
```

---

## Wdrożenie na VPS

### Wymagania

* 1 vCPU i 1 GB RAM wystarczą dla kilkudziesięciu równoczesnych uczestników;
  ~5 GB dysku na obrazy, logi i wolumeny.
* Docker Engine 24+ z wtyczką `docker compose` v2.
* Otwarte porty **80/tcp** (walidacja ACME i przekierowanie na HTTPS),
  **443/tcp** oraz **443/udp** (HTTP/3). Port aplikacji ani Redisa nie są
  publikowane na hoście.
* Domena wskazująca na serwer.

### DNS

Przed pierwszym startem dodaj rekord:

```text
A     poker.example.com    <IPv4 VPS>
AAAA  poker.example.com    <IPv6 VPS>   # jeśli serwer ma IPv6
```

Sprawdź propagację (`dig +short poker.example.com`) — Let's Encrypt zweryfikuje
domenę, więc rekord musi być widoczny publicznie, zanim uruchomisz stack.

### Pierwsze wdrożenie

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2
git clone <repo> /opt/pointy && cd /opt/pointy
cp .env.example .env
${EDITOR:-nano} .env          # APP_DOMAIN, ACME_EMAIL, REDIS_PASSWORD
docker compose up -d --build
docker compose ps             # wszystkie usługi: Up / healthy
curl -fsS https://poker.example.com/healthz
```

Pierwsze wystawienie certyfikatu trwa kilkanaście sekund; postęp widać w
`docker compose logs -f caddy`.

### Aktualizacja

```bash
cd /opt/pointy
git pull
docker compose up -d --build          # przebudowa i restart tylko zmienionych usług
docker image prune -f                 # sprzątanie starych warstw
```

Restart aplikacji nie kasuje pokojów — stan jest w Redisie. Otwarte przeglądarki
same wznowią połączenie WebSocket (klient ponawia je co ~1,5 s).

### Więcej instancji

Procesy nie mają własnego stanu, więc skalują się poziomo:

```bash
docker compose up -d --scale app=3    # dodatkowo APP_WORKERS procesów w każdym
```

Caddy rozwiązuje nazwę `app` na bieżąco (`dynamic a`) i rozkłada ruch metodą
round robin. Sesje nie muszą być przyklejone (*sticky*) — każda instancja czyta
i zapisuje ten sam pokój, a zdarzenia rozsyła kanał pub/sub Redisa.

---

## Eksploatacja

### Healthcheck

`GET /healthz` zwraca `200 {"status":"ok","store":"redis"}` albo
`503 {"status":"degraded", ...}`, gdy Redis nie odpowiada. Używa go healthcheck
kontenera (`docker compose ps` pokazuje `healthy`) oraz aktywne sprawdzanie
upstreamów w Caddym — niesprawna instancja wypada z rotacji.

### Logi

```bash
docker compose logs -f app            # aplikacja: starty, pokoje, błędy domenowe
docker compose logs -f caddy          # HTTP/ACME/TLS
docker compose logs --since 15m app
```

Tokeny sesji nie trafiają do logów: token idzie w **pierwszej ramce WebSocket**,
a nie w URL-u, więc nie ma go ani w logach dostępu Caddy'ego, ani uvicorna.

### Backup

Dane są z założenia ulotne (pokoje wygasają po `ROOM_TTL_SECONDS`), więc backup
sprowadza się do konfiguracji i, opcjonalnie, migawki Redisa:

```bash
# Konfiguracja — to jest to, co naprawdę trzeba mieć.
cp /opt/pointy/.env /bezpieczne/miejsce/pointy.env

# Zrzut Redisa (AOF + RDB w wolumenie redis-data)
docker compose exec redis redis-cli BGSAVE
docker run --rm -v planning-poker_redis-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/redis-$(date +%F).tar.gz -C /data .

# Odtworzenie
docker compose down
docker run --rm -v planning-poker_redis-data:/data -v "$PWD":/backup alpine \
  sh -c 'rm -rf /data/* && tar xzf /backup/redis-2026-01-01.tar.gz -C /data'
docker compose up -d
```

Certyfikaty w wolumenie `caddy-data` też warto zarchiwizować, żeby po migracji
nie trafić na limity Let's Encrypt — ale odtworzą się same.

### Diagnostyka

| objaw | sprawdź |
| --- | --- |
| Brak certyfikatu / `ERR_SSL_...` | `docker compose logs caddy`; DNS musi wskazywać na VPS, a port 80 być otwarty (`sudo ss -tlnp \| grep :80`) |
| `503` na `/healthz` | Redis leży: `docker compose ps redis`, `docker compose logs redis`, `docker compose exec redis redis-cli ping` |
| Uczestnicy nie widzą swoich akcji | sprawdź pub/sub: `docker compose exec redis redis-cli psubscribe 'pp:events'` i wykonaj akcję w przeglądarce |
| WebSocket od razu się rozłącza | `docker compose logs app` — kod błędu domenowego (`unauthorized`, `room_not_found`); zwykle wygasła sesja lub pokój |
| „This room does not exist” | pokój wygasł (`ROOM_TTL_SECONDS`) albo kod jest błędny; `docker compose exec redis redis-cli keys 'pp:room:*'` |
| Ile życia zostało pokojowi | `docker compose exec redis redis-cli ttl pp:room:ABC123` |
| Zużycie pamięci Redisa | `docker compose exec redis redis-cli info memory \| grep used_memory_human` |

Ręczne sprawdzenie upgrade'u WebSocket przez proxy (oczekiwany `101`):

```bash
curl -isS -o /dev/null -w '%{http_code}\n' \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  https://poker.example.com/ws/ABC123
```

---

## Konfiguracja

Wszystko przez zmienne środowiskowe (pełny opis w [.env.example](.env.example)):

| zmienna | domyślnie | znaczenie |
| --- | --- | --- |
| `APP_DOMAIN` | — | domena obsługiwana przez Caddy (`http://localhost` = bez TLS) |
| `ACME_EMAIL` | — | kontakt dla Let's Encrypt |
| `REDIS_PASSWORD` | — | hasło Redisa (tylko sieć wewnętrzna, ale wymagane) |
| `REDIS_URL` | *(puste)* | puste = magazyn w pamięci procesu (dev) |
| `REDIS_KEY_PREFIX` | `pp` | prefiks kluczy i kanału zdarzeń |
| `ROOM_TTL_SECONDS` | `2592000` | bezwzględne życie pokoju i jego historii od utworzenia (30 dni); aktywność go nie odnawia |
| `SESSION_TTL_SECONDS` | `900` | jak długo można wrócić z tą samą tożsamością |
| `CONNECTION_LEASE_SECONDS` | `45` | dzierżawa obecności jednej karty przeglądarki |
| `HEARTBEAT_SECONDS` | `15` | odświeżanie dzierżawy (musi być ≤ połowy dzierżawy) |
| `MAX_PARTICIPANTS` | `50` | limit osób w pokoju |
| `APP_WORKERS` | `2` | procesy uvicorna w kontenerze |
| `LOG_LEVEL` | `INFO` | poziom logów aplikacji i uvicorna |

Sprzeczne ustawienia (np. heartbeat dłuższy niż dzierżawa) zatrzymują start z
czytelnym błędem, zamiast dawać losowo znikających uczestników.

---

## Testy

```bash
.venv/bin/pytest
```

Zakres:

* `tests/test_domain.py` — czyste reguły: głosowanie, statystyki, uprawnienia
  hosta, duplikaty nicków, wygasanie obecności i sesji, serializacja pokoju;
* `tests/test_service.py` — warstwa aplikacyjna na magazynie: równoległe zapisy
  (compare-and-set), rozsyłanie zdarzeń, sprzątanie, wygaśnięcie pokoju;
* `tests/test_api.py` — HTTP i protokół WebSocket end-to-end na ASGI;
* `tests/test_redis_store.py` — kontrakt backendu Redis: współdzielenie stanu
  między instancjami, przetrwanie restartu, TTL, pub/sub. Testy uruchamiają się
  tylko, gdy Redis jest dostępny:

  ```bash
  docker run -d --rm -p 6379:6379 --name pointy-redis redis:7-alpine
  .venv/bin/pytest tests/test_redis_store.py
  ```

Historia ukończonych wycen jest częścią dokumentu pokoju: wszyscy widzą
`zadanie → effort`, ale nie tokeny sesji ani indywidualne głosy. Host zapisuje
wynik po odsłonięciu, może go poprawić lub ponownie wycenić zadanie. Redis/AOF
z wolumenem zachowuje niewygasłe pokoje po restarcie; lokalny `MemoryStore`
jest wyłącznie deweloperski i nie przeżywa restartu. Produkcyjny Redis używa
`noeviction`, więc przy pełnej pamięci zapis powinien być monitorowany zamiast
po cichu usuwać pokoje przed ich 30-dniowym TTL.

Weryfikacja pełnego wdrożenia (proxy + Redis + wiele instancji) przeszła na
`docker compose up -d --scale app=2`: głosowanie, odsłonięcie, nowa runda,
przekazanie hostowania i reconnect działają między uczestnikami obsługiwanymi
przez różne kontenery, a pokój przeżywa `docker compose restart app`.

---

## Architektura i decyzje

* **FastAPI + WebSocket.** REST tylko do utworzenia i dołączenia do pokoju
  (tam powstaje token sesji), reszta idzie stałym połączeniem.
* **Serwer jest źródłem prawdy.** Klient wysyła wyłącznie intencje
  (`vote`, `reveal`, `new_round`). Tożsamość bierze się z tokenu sesji, nigdy z
  treści akcji, a kod pokoju jest walidowany, zanim trafi do klucza w Redisie.
* **Warstwy.** `domain.py` to czyste reguły (bez I/O i bez zegara — czas jest
  argumentem), `store.py` to magazyn (Redis albo pamięć), `service.py` spina
  jedno z drugim, `hub.py` rozsyła stan do lokalnych gniazd, `app.py` to tylko
  transport. Dzięki temu logika testuje się bez sieci i bez Redisa.
* **Jeden dokument na pokój + compare-and-set.** Pokój to jeden klucz JSON;
  każdy zapis to skrypt Lua, który podmienia wartość tylko wtedy, gdy `version`
  się zgadza. Przegrany zapis po prostu wczytuje świeży stan i powtarza zmianę —
  bez blokad rozproszonych i bez ryzyka zgubienia cudzego głosu.
* **Pub/sub zamiast pamięci procesu.** Po udanym zapisie instancja publikuje
  gotowy snapshot na kanale `pp:events`; każda instancja przekazuje go swoim
  gniazdom. Uczestnik podłączony do instancji B natychmiast widzi akcję z
  instancji A. Snapshoty niosą numer wersji, więc spóźniona wiadomość nigdy nie
  cofnie widoku.
* **Obecność jako dzierżawa, nie licznik.** Każda karta przeglądarki ma
  dzierżawę odświeżaną heartbeatem. Zabity proces nie zostawia „wiecznie
  online” uczestników — dzierżawa po prostu wygasa. Kilka kart tej samej osoby
  to jedna obecność, a rozłączenie zeruje jej głos.
* **TTL na dwóch poziomach.** Pokój wraz z historią żyje
  `ROOM_TTL_SECONDS` od utworzenia (domyślnie 30 dni); Redis zachowuje pozostały
  TTL przy każdym zapisie, więc aktywność go nie odnawia. Sesja rozłączonego
  uczestnika znika po `SESSION_TTL_SECONDS`, a porzucony pokój po swoim
  bezwzględnym terminie.
* **Hostowanie.** Twórca zostaje hostem i zachowuje rolę, dopóki jego strona się
  ładuje. Gdy host zniknie, rolę przejmuje najdłużej obecny aktywny uczestnik —
  pokój nigdy nie zostaje bez hosta. Powrót z tym samym tokenem daje tę samą
  tożsamość, bez duplikatu na liście.
* **Bez kont i bez płatnego hostingu.** Dostęp do pokoju daje link plus token
  sesji trzymany w `sessionStorage`.

## Struktura

```text
planning_poker/domain.py   czyste reguły pokoju i głosowania
planning_poker/config.py   konfiguracja ze zmiennych środowiskowych
planning_poker/store.py    magazyn: Redis (CAS + pub/sub) albo pamięć
planning_poker/service.py  load → apply → compare-and-set → publish
planning_poker/hub.py      rozsyłanie stanu do lokalnych WebSocketów
planning_poker/app.py      HTTP, WebSocket, /healthz
planning_poker/static/     interfejs bez ciężkiego frameworka
tests/                     testy jednostkowe, integracyjne i backendu Redis
Dockerfile, docker-compose.yml, Caddyfile, .env.example
```

## Bezpieczeństwo i ograniczenia

* Token sesji nigdy nie pojawia się w URL-u ani w logach; snapshoty rozsyłane do
  przeglądarek nie zawierają tokenów ani cudzych głosów przed odsłonięciem.
* Redis i aplikacja siedzą w wewnętrznej sieci Dockera (`internal: true` dla
  backendu) i nie są publikowane na hoście — z internetu widać wyłącznie Caddy.
* Nie ma rate limitingu ani captchy: każdy z linkiem może utworzyć pokój.
  Ochroną są limity `MAX_PARTICIPANTS`, TTL oraz `maxmemory` Redisa. Przy
  publicznej instancji warto dołożyć limity przed proxy (firewall, fail2ban lub
  Caddy z pluginem `rate_limit`).
* Dostęp do pokoju daje sam link — kto go zna, może dołączyć. To świadoma
  decyzja MVP: bez rejestracji i bez kont.
