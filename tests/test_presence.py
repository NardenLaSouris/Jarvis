"""Présence à la maison : capteurs simulés, moteur arrivée / départ, faux positifs, ordre des événements, sécurité,
persistance, concurrence, journal, résumé d'absence, routine « Bon retour », simulateur (API et ligne de commande).

Horloge simulée, aucun matériel, aucun son : tout passe par le même chemin qu'un vrai capteur (SensorHub.emit).
"""

from __future__ import annotations

import json
import math
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis.events import ALL, Event, EventBus  # noqa: E402
from jarvis.presence import (  # noqa: E402
    ARRIVAL_CONFIRMED, AWAY, DEPARTURE_CONFIRMED, HOME, POSSIBLE_ARRIVAL, POSSIBLE_DEPARTURE, HouseJournal,
    PresenceEngine, PresenceSettings, SensorError, SensorHub, absence_summary, load_sensors,
)
from jarvis.presence.system import build_presence  # noqa: E402

SENSORS = {
    "porte_entree": {"kind": "door", "room": "l'entrée", "entrance": True},
    "mouvement_entree": {"kind": "motion", "room": "l'entrée", "entrance": True},
    "mouvement_salon": {"kind": "motion", "room": "le salon"},
    "telephone_owner": {"kind": "presence", "user": "owner"},
    "telephone_lea": {"kind": "presence", "user": "lea"},
    "machine": {"kind": "appliance", "room": "la buanderie", "name": "la machine à laver"},
    "thermo_salon": {"kind": "temperature", "room": "le salon"},
    "fuite_sdb": {"kind": "leak", "room": "la salle de bain"},
}
T0 = 1_800_000_000.0


class House:
    """Maison simulée : capteurs, bus, moteur ; ``send`` avance l'horloge puis émet le signal."""

    def __init__(self, tmp_path=None, users=("owner",), sensors=None, **settings):
        self.t = T0
        self.bus = EventBus()
        self.events: list[Event] = []
        self.bus.subscribe(ALL, self.events.append)
        table = {k: v for k, v in (sensors or SENSORS).items()
                 if v["kind"] != "presence" or v["user"] in users}
        self.hub = SensorHub(load_sensors(table, set(users) | {"lea"}), self.bus, clock=lambda: self.t)
        tracked = sorted({s.user for s in self.hub.sensors.values() if s.kind == "presence"})
        self.engine = PresenceEngine(tracked, self.bus, PresenceSettings(**settings), clock=lambda: self.t,
                                     state_path=(tmp_path / "presence.json") if tmp_path else None)
        self.engine.attach()

    def send(self, sensor: str, value: str, after: float = 2.0, **extra) -> Event:
        self.t += after
        return self.hub.emit(sensor, value, extra=extra or None)

    def wait(self, seconds: float) -> None:
        self.t += seconds
        self.engine.tick()

    def state(self, user: str = "owner") -> str:
        return self.engine.users[user].state

    def of_type(self, event_type: str) -> list[Event]:
        return [e for e in self.events if e.type == event_type]

    def leave(self) -> None:
        self.send("porte_entree", "open")
        self.send("telephone_owner", "away", after=20)
        self.send("porte_entree", "closed", after=5)
        assert self.state() == AWAY
        self.events.clear()


# --- Scénarios demandés ------------------------------------------------------------------------------------

def test_real_departure():
    house = House()
    house.send("porte_entree", "open")
    assert house.state() == POSSIBLE_DEPARTURE
    house.send("telephone_owner", "away", after=20)
    house.send("porte_entree", "closed", after=5)
    assert house.state() == AWAY
    [departure] = house.of_type(DEPARTURE_CONFIRMED)
    assert departure.payload["confidence"] >= 90 and departure.source == "presence"
    assert house.engine.occupancy() == "empty"


def test_departure_when_the_phone_leaves_the_wifi_after_the_door():
    house = House()
    house.send("porte_entree", "open")
    house.send("porte_entree", "closed", after=5)
    house.send("telephone_owner", "away", after=90)  # le Wi-Fi décroche une minute et demie plus tard
    assert house.state() == AWAY and len(house.of_type(DEPARTURE_CONFIRMED)) == 1


def test_real_return_with_motion():
    house = House()
    house.leave()
    house.send("telephone_owner", "home", after=3600)
    assert house.state() == POSSIBLE_ARRIVAL
    house.send("porte_entree", "open", after=40)
    house.send("mouvement_entree", "detected", after=2)
    house.send("porte_entree", "closed", after=3)
    assert house.state() == HOME
    [arrival] = house.of_type(ARRIVAL_CONFIRMED)
    assert arrival.payload["confidence"] >= 90 and arrival.payload["away_seconds"] > 3000


def test_return_without_motion_is_confirmed_by_phone_and_door():
    house = House()
    house.leave()
    house.send("telephone_owner", "home", after=600)
    house.send("porte_entree", "open", after=60)
    house.send("porte_entree", "closed", after=5)
    assert house.state() == HOME and house.of_type(ARRIVAL_CONFIRMED)[0].payload["confidence"] == 90


@pytest.mark.parametrize("name", ["door_open_then_closed", "parcel"])
def test_door_opened_and_closed_is_never_a_departure(name):
    house = House()
    house.send("porte_entree", "open")
    house.send("porte_entree", "closed", after=30)
    house.wait(600)
    assert house.state() == HOME and not house.of_type(DEPARTURE_CONFIRMED)


def test_short_exit_bins():
    house = House()
    house.send("porte_entree", "open")
    house.send("porte_entree", "closed", after=10)
    house.send("telephone_owner", "home", after=40)  # le téléphone se manifeste : il n'est jamais parti
    assert house.state() == HOME and not house.of_type(DEPARTURE_CONFIRMED)


def test_phone_lost_without_door_is_not_a_departure():
    house = House()
    house.send("telephone_owner", "away")
    assert house.state() == POSSIBLE_DEPARTURE and not house.of_type(DEPARTURE_CONFIRMED)
    house.wait(600)
    assert house.state() == POSSIBLE_DEPARTURE and not house.of_type(DEPARTURE_CONFIRMED)
    house.send("mouvement_salon", "detected", after=60)  # quelqu'un bouge : pas parti
    house.wait(1300)
    assert not house.of_type(DEPARTURE_CONFIRMED)
    assert house.state() == HOME  # le téléphone avait seulement décroché du Wi-Fi


def test_phone_lost_for_long_without_any_motion_becomes_a_departure():
    house = House(absence_timeout=1200)
    house.send("telephone_owner", "away")
    house.wait(1100)
    assert not house.of_type(DEPARTURE_CONFIRMED)
    house.wait(200)
    assert house.state() == AWAY and house.of_type(DEPARTURE_CONFIRMED)[0].payload["reason"] == \
        "absence prolongée sans mouvement"


def test_door_opened_while_the_phone_stays_home():
    house = House()
    house.send("porte_entree", "open")
    house.send("telephone_owner", "home", after=10)
    house.send("porte_entree", "closed", after=10)
    house.wait(600)
    assert house.state() == HOME and not house.of_type(DEPARTURE_CONFIRMED)


def test_false_positive_door_and_motion():
    house = House()
    house.send("porte_entree", "open")
    house.send("mouvement_entree", "detected", after=3)
    house.send("porte_entree", "closed", after=3)
    house.wait(600)
    assert house.state() == HOME and not house.of_type(DEPARTURE_CONFIRMED)


def test_someone_moving_inside_after_the_door_cancels_a_lone_departure():
    house = House()
    house.send("porte_entree", "open")
    house.send("porte_entree", "closed", after=5)
    house.send("mouvement_salon", "detected", after=20)  # encore dans le salon
    house.send("telephone_owner", "away", after=30)  # le Wi-Fi décroche (téléphone en veille)
    assert house.state() != AWAY and not house.of_type(DEPARTURE_CONFIRMED)


def test_door_and_motion_while_away_without_the_phone_is_not_an_arrival():
    house = House()
    house.leave()
    house.send("porte_entree", "open", after=600)
    house.send("mouvement_entree", "detected", after=2)
    house.send("porte_entree", "closed", after=2)
    house.wait(400)
    assert house.state() == AWAY and not house.of_type(ARRIVAL_CONFIRMED)


def test_phone_back_without_door_returns_home_silently():
    house = House()
    house.leave()
    house.send("telephone_owner", "home", after=600)
    house.wait(400)
    assert house.state() == HOME and not house.of_type(ARRIVAL_CONFIRMED)
    assert house.of_type("presence.arrival_unconfirmed")


def test_arrival_cancelled_when_the_phone_leaves_again():
    house = House()
    house.leave()
    house.send("telephone_owner", "home", after=600)
    house.send("telephone_owner", "away", after=30)  # passé devant la maison sans entrer
    assert house.state() == AWAY and not house.of_type(ARRIVAL_CONFIRMED)


# --- Ordre, doublons, anciens et futurs -----------------------------------------------------------------

def test_events_in_the_wrong_order_or_repeated_stay_consistent():
    house = House()
    house.send("porte_entree", "closed")  # fermeture sans ouverture connue
    assert house.state() == HOME
    house.send("porte_entree", "open")
    house.send("porte_entree", "open", after=1)
    house.send("porte_entree", "open", after=1)
    house.send("porte_entree", "closed", after=1)
    house.send("porte_entree", "closed", after=1)
    for _ in range(3):
        house.send("telephone_owner", "home", after=1)
    house.wait(600)
    assert house.state() == HOME and not house.of_type(DEPARTURE_CONFIRMED)
    house.leave()
    for _ in range(3):
        house.send("telephone_owner", "away", after=1)
    assert house.state() == AWAY and not house.of_type(DEPARTURE_CONFIRMED)  # pas de second départ


def test_duplicates_old_and_future_events_are_refused():
    house = House()
    house.hub.emit("porte_entree", "open", timestamp=house.t)
    with pytest.raises(SensorError, match="doublon"):
        house.hub.emit("porte_entree", "open", timestamp=house.t)
    with pytest.raises(SensorError, match="ancien"):
        house.hub.emit("telephone_owner", "away", timestamp=house.t - 3600)
    with pytest.raises(SensorError, match="futur"):
        house.hub.emit("telephone_owner", "away", timestamp=house.t + 3600)
    for bad in (math.nan, math.inf, "hier", True):
        with pytest.raises(SensorError, match="horodatage"):
            house.hub.emit("telephone_owner", "away", timestamp=bad)
    assert house.state() == POSSIBLE_DEPARTURE and not house.of_type(DEPARTURE_CONFIRMED)


def test_very_close_events_are_handled():
    house = House()
    house.send("porte_entree", "open", after=0.01)
    house.send("telephone_owner", "away", after=0.01)
    house.send("porte_entree", "closed", after=0.01)
    assert house.state() == AWAY


# --- Sécurité ---------------------------------------------------------------------------------------------

def test_unknown_sensor_bad_values_and_payloads_are_refused():
    house = House()
    with pytest.raises(SensorError, match="inconnu"):
        house.hub.emit("porte_garage", "open")
    with pytest.raises(SensorError, match="invalide"):
        house.hub.emit("porte_entree", "explose")
    with pytest.raises(SensorError, match="température"):
        house.hub.emit("thermo_salon", "measured", extra={"celsius": "chaud"})
    with pytest.raises(SensorError, match="température"):
        house.hub.emit("thermo_salon", "measured", extra={"celsius": 900})
    assert not any(e.type.startswith(("door.", "temperature.")) for e in house.events)


def test_simulation_cannot_drive_a_real_sensor_and_forged_events_are_ignored(monkeypatch):
    from jarvis.presence import sensors as sensors_module

    monkeypatch.setitem(sensors_module.DRIVERS, "mqtt", sensors_module.SimulatedDriver)
    table = {**SENSORS, "porte_entree": {**SENSORS["porte_entree"], "driver": "mqtt"}}
    house = House(sensors=table)
    with pytest.raises(SensorError, match="refusé"):
        house.hub.emit("porte_entree", "open", source="simulated")
    house.hub.emit("porte_entree", "open", source="mqtt")  # le vrai pilote, lui, est accepté
    assert house.state() == POSSIBLE_DEPARTURE
    # Événement forgé sur le bus par autre chose qu'un capteur : ignoré par le moteur.
    house.bus.publish(Event("presence.away", "intrus", {"user": "owner", "sensor": "telephone_owner"}))
    house.bus.publish(Event("door.closed", "intrus", {"sensor": "porte_entree", "entrance": True}))
    assert house.state() == POSSIBLE_DEPARTURE and not house.of_type(DEPARTURE_CONFIRMED)


def test_invalid_sensor_configuration_names_the_key():
    with pytest.raises(SensorError, match=r"\[presence.sensors.tel\] user"):
        load_sensors({"tel": {"kind": "presence", "user": "inconnu"}}, {"owner"})
    with pytest.raises(SensorError, match="kind"):
        load_sensors({"x": {"kind": "teleporteur"}}, {"owner"})
    with pytest.raises(SensorError, match="driver"):
        load_sensors({"x": {"kind": "door", "driver": "zigbee_magique"}}, {"owner"})
    with pytest.raises(SensorError, match="identifiant"):
        load_sensors({"Porte Entrée": {"kind": "door"}}, {"owner"})


def test_forged_arrival_never_triggers_a_routine():
    from test_routines import Setup

    setup = Setup()
    routine = setup.engine.create({"name": "Bon retour", "trigger": {"type": "event", "event": "arrival"},
                                   "actions": [{"type": "say", "text": "Bon retour."}]})
    setup.engine.attach_events(setup.events)
    setup.events.publish(Event(ARRIVAL_CONFIRMED, "intrus", {"user": "owner"}))
    assert setup.engine.get(routine["id"])["last_run"] is None


# --- Plusieurs personnes -------------------------------------------------------------------------------

def test_house_occupied_by_someone_else_does_not_bring_the_owner_home():
    house = House(users=("owner", "lea"))
    house.send("porte_entree", "open")
    house.send("telephone_owner", "away", after=10)
    house.send("porte_entree", "closed", after=5)
    assert house.state("owner") == AWAY and house.state("lea") in (HOME, POSSIBLE_DEPARTURE)
    house.wait(600)
    assert house.state("lea") == HOME and house.engine.occupancy() == "occupied"
    house.send("mouvement_salon", "detected", after=60)
    house.send("porte_entree", "open", after=60)
    house.send("porte_entree", "closed", after=5)
    house.wait(600)
    assert house.state("owner") == AWAY and not house.of_type(ARRIVAL_CONFIRMED)
    assert house.engine.is_home("owner") is False and house.engine.is_home("lea") is True
    assert house.engine.is_home("inconnu") is None


# --- Persistance, redémarrage, concurrence -------------------------------------------------------------

def test_state_survives_a_restart_but_not_an_unfinished_hypothesis(tmp_path):
    house = House(tmp_path)
    house.leave()
    again = House(tmp_path)
    assert again.state() == AWAY and again.engine.users["owner"].away_since is not None
    again.send("telephone_owner", "home", after=600)
    assert again.state() == POSSIBLE_ARRIVAL
    third = House(tmp_path)  # hypothèse d'arrivée non confirmée au redémarrage : on reste absent
    assert third.state() == AWAY
    (tmp_path / "presence.json").write_text("{abîmé", encoding="utf-8")
    assert House(tmp_path).state() == HOME  # fichier illisible mis de côté, JARVIS repart


def test_concurrent_signals_keep_a_consistent_state():
    house = House(users=("owner", "lea"))
    errors = []

    def burst(sensor, values):
        for i, value in enumerate(values * 20):
            try:
                house.hub.emit(sensor, value, timestamp=house.t - i * 0.001)
            except SensorError:
                pass
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=burst, args=args) for args in
               (("porte_entree", ["open", "closed"]), ("telephone_lea", ["home", "away"]),
                ("mouvement_salon", ["detected"]), ("telephone_owner", ["home"]))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    house.wait(2000)
    assert not errors and all(u.state in (HOME, AWAY) for u in house.engine.users.values())


# --- Journal et résumé d'absence ------------------------------------------------------------------------

def journal_house(tmp_path):
    house = House(tmp_path)
    journal = HouseJournal(tmp_path / "journal.jsonl", clock=lambda: house.t)
    journal.attach(house.bus)
    return house, journal


def test_journal_keeps_what_matters_and_survives_a_restart(tmp_path):
    house, journal = journal_house(tmp_path)
    house.leave()
    for _ in range(20):
        house.send("mouvement_salon", "detected", after=1)
    house.send("machine", "finished", after=60)
    house.send("thermo_salon", "measured", after=60, celsius=19.0)
    house.send("thermo_salon", "measured", after=60, celsius=19.2)  # trop proche : non gardée
    house.send("thermo_salon", "measured", after=60, celsius=21.4)
    entries = HouseJournal(tmp_path / "journal.jsonl").entries()  # relu comme après un redémarrage
    types = [e["type"] for e in entries]
    assert "motion.detected" not in types and "door.closed" not in types
    assert types.count("temperature.measured") == 2 and "appliance.finished" in types
    assert any(e["text"] == "La machine à laver a terminé son cycle." and e["priority"] == "IMPORTANT"
               for e in entries)
    assert "presence.departure_confirmed" in types


def test_absence_summary_selects_and_never_alarms_for_nothing(tmp_path):
    house, journal = journal_house(tmp_path)
    house.leave()
    left = house.t
    house.send("thermo_salon", "measured", after=60, celsius=19.0)
    house.wait(1000)
    assert absence_summary(journal.entries(), left, house.t) == "Rien de particulier pendant votre absence."
    house.send("machine", "finished", after=60)
    house.send("thermo_salon", "measured", after=600, celsius=21.1)
    house.send("fuite_sdb", "detected", after=60)
    summary = absence_summary(journal.entries(), left, house.t)
    assert summary.startswith("Une fuite d'eau a été détectée dans la salle de bain.")  # critique d'abord
    assert "La machine à laver a terminé son cycle." in summary
    assert "La température du salon a augmenté de deux degrés." in summary
    assert "mouvement" not in summary.lower()


def test_absence_summary_ignores_the_arrival_itself_but_not_an_earlier_door(tmp_path):
    house, journal = journal_house(tmp_path)
    house.leave()
    left = house.t
    house.send("porte_entree", "open", after=1800)  # quelqu'un est passé pendant l'absence
    house.send("porte_entree", "closed", after=10)
    house.send("telephone_owner", "home", after=1800)
    arrival = house.t
    house.send("porte_entree", "open", after=30)
    house.send("porte_entree", "closed", after=5)
    summary = absence_summary(journal.entries(), left, house.t, quiet_before=house.t - arrival + 5)
    assert summary == "La porte de l'entrée a été ouverte pendant votre absence."


# --- Système complet : simulateur, routine « Bon retour » ----------------------------------------------

def presence_config(tmp_path, **overrides):
    values = dict(departure_window=180.0, arrival_window=300.0, threshold=90, absence_minutes=20.0,
                  door_left_open_minutes=10.0, max_event_age=120.0, welcome_routine=True, absence_summary=True,
                  state_path=tmp_path / "presence.json", journal_path=tmp_path / "journal.jsonl",
                  journal_max_kb=100, sensors=SENSORS)
    values.update(overrides)
    return SimpleNamespace(**values)


def test_welcome_routine_runs_only_on_a_confirmed_arrival(tmp_path):
    from jarvis.routines import MemoryRoutineStore, RoutineEngine
    from jarvis.routines.announce import Announcer
    from test_tools import make_core

    clock = {"t": T0}
    bus = EventBus()
    system = build_presence(presence_config(tmp_path), bus, {"owner", "lea"}, clock=lambda: clock["t"])
    assert system.first_start
    said = []
    announcer = Announcer(lambda d: None, lambda: [],
                          welcome=lambda ctx: system.welcome(ctx, lambda user: "Bon retour, monsieur."))
    core = make_core()
    engine = RoutineEngine(MemoryRoutineStore([]), core.registry, core.submit, lambda title, text: said.append(text),
                           bus, sleep=lambda s: None, announce=announcer.text)
    engine.attach_events(bus)
    engine.create({"name": "Bon retour", "trigger": {"type": "event", "event": "arrival"},
                   "actions": [{"type": "announce", "what": "welcome"}]})

    def send(sensor, value, after=2.0, **extra):
        clock["t"] += after
        system.simulate(sensor, value, extra or None)

    def settle():
        for thread in threading.enumerate():
            if thread.name.startswith("routine-"):
                thread.join(2)

    send("door", "open")  # par sorte de capteur, comme le simulateur
    send("door", "close", after=20)
    settle()
    assert said == []  # porte seule : aucun accueil
    send("telephone_owner", "away", after=10)
    send("machine", "termine", after=1800)
    send("telephone_owner", "home", after=1800)
    send("porte_entree", "open", after=30)
    settle()
    assert said == []  # téléphone et porte ouverte (80) : pas encore confirmé
    send("mouvement_entree", "detected", after=2)
    settle()
    assert said == ["Bon retour, monsieur. La machine à laver a terminé son cycle."]
    assert said[0].count("monsieur") == 1
    send("porte_entree", "closed", after=3)
    settle()
    assert len(said) == 1  # une seule fois par retour


def test_simulator_through_the_admin_api_and_the_command_line(tmp_path, monkeypatch, capsys):
    from jarvis.__main__ import simulate
    from test_api import TOKEN, World

    world = World(tmp_path)
    bus = EventBus()
    system = build_presence(presence_config(tmp_path), bus, {"owner", "lea"})
    world.api.presence = system
    try:
        status, data = world.call("POST", "/presence/simulate", {"sensor": "door", "value": "open"})
        assert status == 200 and data["event"]["type"] == "door.open"
        assert data["presence"]["users"]["owner"]["state"] == POSSIBLE_DEPARTURE
        assert world.call("POST", "/presence/simulate", {"sensor": "garage", "value": "open"})[0] == 400
        assert world.call("POST", "/presence/simulate", {"sensor": 3})[0] == 400
        assert world.call("POST", "/presence/simulate", {"sensor": "porte_entree", "value": "open"},
                          token="mauvais")[0] == 401
        status, data = world.call("GET", "/presence")
        assert status == 200 and data["house"] == "occupied" and "porte_entree" in data["sensors"]
        # Ligne de commande : même chemin, par l'API sur cette machine.
        monkeypatch.setenv("JARVIS_AGENT_TOKEN", TOKEN)
        cfg = SimpleNamespace(api=SimpleNamespace(enabled=True, port=world.api.address[1]))
        assert simulate(cfg, ["telephone_owner", "away"]) == 0
        assert simulate(cfg, ["door", "close"]) == 0
        assert "owner : AWAY" in capsys.readouterr().out
        assert simulate(cfg, ["status"]) == 0 and "Départ détecté" in capsys.readouterr().out
        assert simulate(cfg, ["door"]) == 2
    finally:
        world.api.stop()


def test_presence_question_by_voice(tmp_path):
    from jarvis.presence.tools import presence_tool
    from jarvis.tools.quick import quick_plan
    from test_quick import full_core

    house = House(users=("owner", "lea"))
    core = full_core()
    core.registry.register(presence_tool(house.engine, lambda u: "vous" if u == "owner" else "Léa"))
    assert quick_plan("Qui est à la maison ?", core.registry)["tool"] == "presence_status"
    result = core.submit({"tool": "presence_status", "parameters": {}}).result
    assert result.message == "À la maison : vous, Léa."
    house.leave()
    result = core.submit({"tool": "presence_status", "parameters": {}}).result
    assert result.message == "À la maison : Léa. Absent : vous."


def test_event_trigger_is_validated():
    from jarvis.routines.model import RoutineError, parse_trigger

    assert parse_trigger({"type": "event", "event": "arrival"}) == {"type": "event", "event": "arrival"}
    assert parse_trigger({"type": "event", "event": "departure", "user": "lea"})["user"] == "lea"
    for bad in ({"type": "event", "event": "door.open"}, {"type": "event"},
                {"type": "event", "event": "arrival", "user": "Léa Martin"}):
        with pytest.raises(RoutineError):
            parse_trigger(bad)


def test_door_left_open_is_noted_once():
    house = House(door_left_open=600)
    house.send("porte_entree", "open")
    house.wait(500)
    assert not house.of_type("door.left_open")
    house.wait(200)
    house.wait(200)
    assert len(house.of_type("door.left_open")) == 1
