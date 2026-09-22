-- Схема хранилища телеметрии. Применяется коллектором при старте.
--
-- Три таблицы: сырые события и два суточных свода. Сырое живёт 90 дней и
-- чистится, своды остаются навсегда — они занимают копейки и дают то, чего
-- сейчас нет совсем: историю, по которой через полгода можно будет сказать
-- «этим стали пользоваться вдвое меньше», не начиная новое двухмесячное
-- наблюдение.

CREATE TABLE IF NOT EXISTS events (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL,
    service     text        NOT NULL,
    source      text        NOT NULL,
    method      text        NOT NULL,
    path        text        NOT NULL,
    status      smallint    NOT NULL,
    duration_ms integer     NOT NULL,
    auth        text        NOT NULL,
    -- Идентификатор из той авторизации, которая в приложении уже есть.
    -- Имени и почты здесь нет намеренно: это метрика приложений.
    user_id     text,
    session_id  text,
    user_agent  text
);

CREATE INDEX IF NOT EXISTS events_at_idx ON events (at);
CREATE INDEX IF NOT EXISTS events_service_at_idx ON events (service, at);

-- Человеко-дни: «Иванов заходил в МПП2 четырнадцать дней из шестидесяти».
-- Единица счёта выбрана осознанно вместо сессий по таймауту: на шкале
-- «живое / мёртвое» она устойчивее и не порождает спора о методике в тот
-- момент, когда кто-то не согласится с выключением своего приложения.
CREATE TABLE IF NOT EXISTS daily_users (
    day     date NOT NULL,
    service text NOT NULL,
    auth    text NOT NULL,
    user_id text NOT NULL,
    hits    integer NOT NULL,
    PRIMARY KEY (day, service, auth, user_id)
);

CREATE INDEX IF NOT EXISTS daily_users_service_day_idx ON daily_users (service, day);

-- Разделы и данные: что внутри приложения читают, а что считается зря.
CREATE TABLE IF NOT EXISTS daily_paths (
    day          date NOT NULL,
    service      text NOT NULL,
    method       text NOT NULL,
    path         text NOT NULL,
    hits         integer NOT NULL,
    users        integer NOT NULL,
    errors       integer NOT NULL,
    duration_p95 integer NOT NULL,
    PRIMARY KEY (day, service, method, path)
);

CREATE INDEX IF NOT EXISTS daily_paths_service_day_idx ON daily_paths (service, day);

-- Отметки о проходах свода: по ним видно, что агрегация жива, и с какого
-- момента пересчитывать после простоя.
CREATE TABLE IF NOT EXISTS aggregate_runs (
    id         bigserial PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT now(),
    days       integer NOT NULL,
    rows_users integer NOT NULL,
    rows_paths integer NOT NULL,
    deleted    integer NOT NULL
);

-- Справочник приложений: машинный код и человеческое имя.
--
-- Наполняется из status_page_services в инвентаре — того же списка, по
-- которому живёт статус-страница. Третий независимый список названий не
-- заводится: через полгода он дал бы три разных имени одного сервиса.
--
-- Нужен ради вопроса, на который сами события ответить не могут: приложение,
-- которое молчит, в событиях не появляется вовсе, и без списка ожидаемых
-- «мёртвый» неотличим от «сбор не раскатан».
CREATE TABLE IF NOT EXISTS services (
    code     text PRIMARY KEY,
    title    text NOT NULL,
    expected boolean NOT NULL DEFAULT true,
    seen_at  timestamptz
);
