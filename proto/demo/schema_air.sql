-- CymbalAir demo schema (Airplane Alternatives CUJ, wildfire rfc1)
DROP TABLE IF EXISTS seats CASCADE;
DROP TABLE IF EXISTS bookings CASCADE;
DROP TABLE IF EXISTS flights CASCADE;
DROP TABLE IF EXISTS wf_applied CASCADE;

CREATE TABLE flights (
    id          bigint PRIMARY KEY,
    flight_no   text NOT NULL,
    origin      text NOT NULL,
    dest        text NOT NULL,
    departs_at  timestamptz NOT NULL,
    status      text NOT NULL DEFAULT 'scheduled'   -- scheduled | cancelled
);

CREATE TABLE bookings (
    id          text PRIMARY KEY,        -- client-generated (avoids sequence collision on merge)
    flight_id   bigint NOT NULL REFERENCES flights(id),
    passenger   text NOT NULL,
    seat_no     text,
    price       numeric(10,2) NOT NULL,
    status      text NOT NULL DEFAULT 'confirmed',  -- confirmed | cancelled
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE seats (
    id          bigint PRIMARY KEY,
    flight_id   bigint NOT NULL REFERENCES flights(id),
    seat_no     text NOT NULL,
    cabin       text NOT NULL DEFAULT 'economy',
    status      text NOT NULL DEFAULT 'available',  -- available | booked
    booking_id  text
);

CREATE TABLE wf_applied (
    action_id   text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
