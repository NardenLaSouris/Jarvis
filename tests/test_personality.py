"""Tests de la personnalité et du routeur d'intentions (sans modèle, sans réseau).

Lancement : python tests/test_personality.py
"""

from __future__ import annotations

import random
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.agent import Agent, AgentSettings  # noqa: E402
from jarvis.audio.endpointing import EndpointerSettings, UtteranceRecorder  # noqa: E402
from jarvis.audio.files import ArraySource, RecordingSink  # noqa: E402
from jarvis.capabilities import CapabilityRegistry  # noqa: E402
from jarvis.personality import load_personality, normalize  # noqa: E402
from jarvis.router import IntentRouter, claims_action  # noqa: E402

PERSONALITY = load_personality(ROOT / "personality.toml")
MORNING = datetime(2026, 9, 24, 9, 0)
EVENING = datetime(2026, 9, 24, 21, 0)
TUTOIEMENT = re.compile(r"\b(tu|toi|ton|ta|tes|te|t')\b", re.IGNORECASE)


def router(clock=MORNING, capabilities=None, personality=PERSONALITY):
    return IntentRouter(personality, capabilities or CapabilityRegistry(), random.Random(0), lambda: clock)


def test_normalization():
    assert normalize("  Qu'est-ce QUE tu es ?! ") == "qu est ce que tu es"
    assert normalize("Éteins la lumière, s'il te plaît") == "eteins la lumiere s il te plait"
    assert PERSONALITY.strip_ignored(normalize("Merci beaucoup, Jarvis !")) == "merci beaucoup"


def test_expected_intents():
    cases = {
        "Qui es-tu ?": "identity",
        "Tu es qui ?": "identity",
        "Présente-toi": "identity",
        "Qu'est-ce que tu es ?": "identity",
        "Quel est ton rôle ?": "identity",
        "Jarvis, tu peux me dire qui tu es ?": "identity",
        "Bonjour": "greeting",
        "Bonjour Jarvis": "greeting",
        "Salut Jarvis": "greeting",
        "Bonsoir Jarvis": "greeting",
        "Merci": "thanks",
        "Merci Jarvis": "thanks",
        "Merci beaucoup": "thanks",
        "Au revoir": "goodbye",
        "À plus tard": "goodbye",
        "Bonne nuit": "goodnight",
        "Comment tu t'appelles ?": "identity",
        "Quel est ton nom ?": "identity",
        "Qui t'a créé ?": "creator",
        "Qui t'a conçu ?": "creator",
        "Que peux-tu faire ?": "capabilities",
        "Stop": "stop",
    }
    r = router()
    for text, expected in cases.items():
        assert r.route(text).name == expected, (text, r.route(text))


def test_mission_identity_phrasings_including_stt_errors():
    r = router()
    phrasings = [
        "Qui es-tu ?", "qui est tu ?", "Qui est-tu ?", "QUI EST TU !", "qui est-tu", "Tu es qui ?", "Tu es quoi ?",
        "C'est quoi ton nom ?", "Comment tu t'appelles ?", "Quel est ton nom ?", "Présente-toi", "Présente toi",
        "Tu peux te présenter ?", "Qu'est-ce que tu es ?", "Qu'est ce que tu es ?", "Quel est ton rôle ?",
        "Tu fais quoi ?", "Jarvis, qui es-tu ?", "qui et tu", "présante toi",
    ]
    for text in phrasings:
        assert r.route(text).label == "predefined:identity", (text, r.route(text))


def test_mission_other_intents_and_llm():
    r = router()
    assert r.route("Bonjour").label == "predefined:greeting"
    assert r.route("Merci").label == "predefined:thanks"
    assert r.route("Au revoir").label == "predefined:goodbye"
    assert r.route("Que peux-tu faire ?").label == "predefined:capabilities"
    assert r.route("Quelle est la capitale de la France ?").label == "llm"


def test_tolerance_is_not_too_permissive():
    r = router()
    for text in ("Qui est cette personne ?", "Quel est son nom ?", "Qui est le président de la France ?",
                 "Mercredi", "Top", "Tu es là ?", "Comment il s'appelle ?"):
        assert r.route(text).label == "llm", (text, r.route(text))


def test_identity_reply_is_short_and_distinct_from_capabilities():
    r = router()
    for _ in range(6):
        reply = r.route("Qui es-tu ?").reply
        assert len(reply.split()) <= 12 and "JARVIS" in reply, reply
        assert "converser" not in reply and "Jules" not in reply
    assert "converser" in r.route("Que peux-tu faire ?").reply


def test_everything_else_goes_to_llm():
    r = router()
    for text in ("Quelle est la capitale de l'Australie ?", "Merci de me dire la capitale de l'Italie",
                 "Raconte-moi une blague", "La lumière de Paris est magnifique",
                 "Ferme les yeux et imagine un voyage"):
        route = r.route(text)
        assert route.source == "llm" and route.reply is None, (text, route)


def test_unavailable_actions_are_answered_without_the_llm():
    r = router()
    cases = {
        "Éteins la lumière du salon": ("unavailable_home", "domotique"),
        "Allume le chauffage à 21 degrés": ("unavailable_home", "domotique"),
        "Envoie un message à Paul pour lui dire que j'arrive": ("unavailable_messages", "messages"),
        "Ouvre le navigateur": ("unavailable_computer", "ordinateur"),
        "Quelle est la météo demain ?": ("unavailable_realtime", "temps réel"),
        "Tu peux programmer mon réveil à 7 heures ?": ("unavailable_reminders", "rappels"),
    }
    for text, (name, word) in cases.items():
        route = r.route(text)
        assert route.source == "unavailable" and route.name == name, (text, route)
    assert "domotique" in r.route("Éteins la lumière du salon").reply or "maison" in r.route("Éteins la lumière").reply


def test_critical_commands_come_first():
    route = router().route("Stop")
    assert route.label == "critical:stop" and route.end_conversation


def test_predefined_responses_are_rendered():
    r = router()
    assert r.route("Comment tu t'appelles ?").reply.split(",")[0] in ("Je suis JARVIS", "JARVIS")
    assert r.route("Qui t'a créé ?").reply.startswith(("Vous", "C'est vous"))
    capabilities = r.route("Que peux-tu faire ?").reply
    assert "converser" in capabilities
    assert "{" not in capabilities


def test_salutation_follows_the_time_of_day():
    assert router(MORNING).route("Bonsoir").reply.startswith("Bonjour")
    assert router(EVENING).route("Bonjour").reply.startswith("Bonsoir")


def test_goodbye_ends_the_conversation():
    assert router().route("Au revoir").end_conversation
    assert not router().route("Quelle heure est-il ?").end_conversation


def test_variants_never_repeat_twice_in_a_row():
    r = router()
    replies = [r.route("Merci").reply for _ in range(60)]
    assert all(a != b for a, b in zip(replies, replies[1:]))
    assert len(set(replies)) > 1


def test_replies_use_the_title_naturally_and_never_tutoie():
    r = router()
    rendered = [r._render(t) for intent in PERSONALITY.intents if intent.name != "who_is_user"
                for t in intent.responses]
    rendered += [r._render(t) for phrases in PERSONALITY.phrases.values() for t in phrases]
    for reply in rendered:
        assert not TUTOIEMENT.search(reply), reply
        assert "Jules" not in reply, reply
        assert reply.lower().count("monsieur") <= 1, reply
    for intent in ("thanks", "how_are_you", "identity", "greeting", "goodbye"):
        replies = [r._render(t) for i in PERSONALITY.intents if i.name == intent for t in i.responses]
        assert any("monsieur" not in x for x in replies), intent


def test_title_comes_from_configuration():
    toml = (ROOT / "personality.toml").read_text(encoding="utf-8").replace('user_title = "monsieur"', 'user_title = "patron"')
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "personality.toml"
        path.write_text(toml, encoding="utf-8")
        custom = load_personality(path)
    reply = router(personality=custom).route("Comment tu t'appelles ?").reply
    assert "patron" in reply and "monsieur" not in reply


def test_false_action_claims_are_detected():
    for claim in ("La lumière du salon est éteinte.", "J'ai éteint la lumière, monsieur.",
                  "C'est fait, monsieur.", "Voilà, j'ai lancé la musique.", "Message envoyé.",
                  "Je m'en occupe, monsieur.", "Le chauffage sera mis à 21 degrés demain matin.",
                  "Je vais allumer la lumière.", "Bien sûr, je peux programmer votre réveil à 7 heures.",
                  "J'ai pris note de votre demande.", "Je vous rappellerai dès que ce sera disponible."):
        assert claims_action(claim), claim
    for honest in ("Je pourrais m'en charger, monsieur, mais la domotique n'est pas encore disponible.",
                   "Je ne peux pas éteindre la lumière pour le moment.",
                   "La capitale de l'Australie est Canberra.",
                   "J'ai bien compris votre demande, monsieur.",
                   "Je pourrais m'en charger, monsieur, mais ce n'est pas encore possible."):
        assert not claims_action(honest), honest


def test_false_action_reply_is_replaced():
    r = router()
    reply = r.check_reply("Bien sûr, monsieur. La lumière du salon est maintenant éteinte.")
    assert "pas encore disponible" in reply
    assert r.check_reply("La capitale de l'Australie est Canberra.") == "La capitale de l'Australie est Canberra."
    assert r.check_reply("J'ai éteint la lumière.", tools_used=True) == "J'ai éteint la lumière."
    assert r.check_reply("Parfait, Monsieur. Canberra.") == "Parfait, monsieur. Canberra."
    assert r.check_reply("Monsieur, voici la réponse.") == "Monsieur, voici la réponse."
    assert r.check_reply("Je suis désolé, Monsieur, mais non.") == "Je suis désolé, monsieur, mais non."


def test_filler_openings_are_trimmed_from_llm_replies():
    r = router()
    assert r.check_reply("Bien sûr, monsieur. La capitale de la France est Paris.") == "La capitale de la France est Paris."
    assert r.check_reply("Certainement. Paris.") == "Paris."
    assert r.check_reply("Bien sûr, monsieur.") == "Bien sûr, monsieur."
    assert r.check_reply("Bien sûr que Paris est la capitale.") == "Bien sûr que Paris est la capitale."
    assert r.check_reply("Bien sûr, Monsieur, la capitale est Paris.") == "La capitale est Paris."


def test_truncated_llm_reply_ends_on_a_complete_sentence():
    r = router()
    assert r.check_reply("Évitez les écrans. Couchez-vous à heure fixe. Essayez un régime de sommeil régul") ==         "Évitez les écrans. Couchez-vous à heure fixe."
    assert r.check_reply("Victor Hugo, monsieur.") == "Victor Hugo, monsieur."


def test_how_are_you_is_small_talk_not_llm():
    assert router().route("Comment ça va ?").label == "predefined:how_are_you"
    assert router().route("Ça va Jarvis ?").label == "predefined:how_are_you"
    assert router().route("Ça va être long ?").label == "llm"


def test_mission_conversation_sequence():
    r = router()
    r.start_conversation()
    assert r.route("Comment vas-tu ?").reply in ("Très bien, merci. Et vous ?", "Parfaitement bien, merci. Et vous-même ?",
                                               "Tous mes systèmes fonctionnent à merveille, monsieur. Et vous ?")
    assert r.route("Moi ça va.").label == "predefined:user_doing_well"
    assert r.route("Quelle est la capitale de la France ?").label == "llm"
    identity = r.route("Qui es-tu ?")
    assert identity.label == "predefined:identity" and "JARVIS" in identity.reply


def test_user_answer_is_only_understood_right_after_how_are_you():
    r = router()
    r.start_conversation()
    assert r.route("Moi ça va.").label == "llm"
    r.route("Comment vas-tu ?")
    r.route("Quelle heure est-il ?")
    assert r.route("Moi ça va.").label == "llm"
    r.route("Comment vas-tu ?")
    r.start_conversation()
    assert r.route("Moi ça va.").label == "llm"


def test_user_identity_is_not_confused_with_jarvis():
    r = router()
    who = r.route("Qui est Jules ?")
    assert who.label == "predefined:who_is_user" and "Jules" not in who.reply
    for _ in range(6):
        assert "Jules" not in r.route("Qui es-tu ?").reply


def test_llm_replies_do_not_open_with_greetings_or_formulas():
    r = router()
    assert r.check_reply("Bonjour, monsieur. La capitale de la France est Paris.", "Quelle est la capitale ?") ==         "La capitale de la France est Paris."
    assert r.check_reply("Bonjour, monsieur. Je suis heureux de vous aider. Paris.", "capitale ?") == "Paris."
    assert r.check_reply("Bonsoir, monsieur. Il est 21 heures.", "Bonsoir, quelle heure est-il ?") ==         "Bonsoir, monsieur. Il est 21 heures."


def test_llm_replies_drop_empty_closing_formulas():
    r = router()
    assert r.check_reply("Réinitialisez-le depuis la page de connexion. N'hésitez pas à me demander.") ==         "Réinitialisez-le depuis la page de connexion."
    assert r.check_reply("N'hésitez pas à me demander.") == "N'hésitez pas à me demander."


def test_title_used_at_most_once_in_llm_replies():
    r = router()
    reply = r.check_reply("Paris, monsieur. C'est une belle ville, monsieur, n'est-ce pas monsieur ?")
    assert reply == "Paris, monsieur. C'est une belle ville, n'est-ce pas ?"


def test_prompt_describes_personality_capabilities_and_forbids_fake_actions():
    prompt = router().system_prompt()
    for expected in ("Nom de l'assistant : JARVIS", "Utilisateur principal : la personne qui vous parle", "Forme d'adresse : « monsieur »",
                     "Langue : français", "ne le tutoyez jamais", "aucune", "domotique", "Aucun outil",
                     "n'écrivez donc jamais qu'une action est faite", "au plus une", "Ne commencez jamais par une salutation",
                     "« Paris. »", "C'est le début de l'échange"):
        assert expected in prompt, expected
    assert "déjà engagée" in router().system_prompt(ongoing=True)


class FakeCapability:
    name = "lumières"
    description = "allumer et éteindre les lumières"

    def handle(self, text):
        return "Lumière éteinte, monsieur." if "lumiere" in normalize(text) else None


def test_capabilities_are_tried_before_the_llm():
    registry = CapabilityRegistry()
    registry.register(FakeCapability())
    route = router(capabilities=registry).route("Éteins la lumière du salon")
    assert route.source == "capability" and route.reply == "Lumière éteinte, monsieur."
    assert "allumer et éteindre les lumières" in router(capabilities=registry).system_prompt()


def test_agent_answers_predefined_intents_without_the_llm():
    class Stt:
        def __init__(self):
            self.replies = iter(["Qui es-tu ?", "Quelle est la capitale de l'Australie ?", "Au revoir"])

        def transcribe(self, audio, rate):
            return next(self.replies)

    class Llm:
        calls = []

        def chat(self, messages):
            self.calls.append(messages)
            return "La capitale de l'Australie est Canberra, monsieur."

    class Tts:
        def synthesize(self, text):
            return np.zeros(10, np.int16), 16000

    class Wake:
        def process(self, frame):
            return 1.0 if np.abs(frame).max() > 20000 else 0.0

        def reset(self):
            pass

    sr, speech = 16000, lambda s: (3000 * np.sin(np.arange(int(s * 16000)) / 16000 * 1400)).astype(np.int16)
    silence = lambda s: np.zeros(int(s * sr), np.int16)
    audio = np.concatenate([silence(1), np.full(3200, 30000, np.int16), silence(0.5),
                            speech(1), silence(1.5), speech(1), silence(1.5), speech(1), silence(1.5), speech(1), silence(3)])
    source, sink, llm, events = ArraySource(audio, sr, 1280), RecordingSink(), Llm(), []
    settings = AgentSettings("JARVIS", "Jarvis", 0.5, ("Oui, monsieur ?",), 3.0, 2.0, 6)
    agent = Agent(settings, source, sink, Wake(), UtteranceRecorder(source, EndpointerSettings()),
                  Stt(), llm, Tts(), router(), lambda kind, text: events.append((kind, text)))
    agent.run()
    routes = [text for kind, text in events if kind == "routing"]
    assert routes == ["predefined:identity", "llm", "predefined:goodbye"]
    assert sum(1 for kind, text in events if kind == "timing" and text.startswith("LLM total")) == 1
    assert len(llm.calls) == 1
    assert [m.role for m in llm.calls[0]][-3:] == ["user", "assistant", "user"]
    users = [text for kind, text in events if kind == "user"]
    assert users == ["Qui es-tu ?", "Quelle est la capitale de l'Australie ?", "Au revoir"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)


def test_date_and_time_questions_are_answered_without_the_llm():
    from datetime import datetime

    from jarvis.capabilities import CapabilityRegistry
    from jarvis.personality import load_personality
    from jarvis.router import IntentRouter

    router = IntentRouter(load_personality(ROOT / "personality.toml"), CapabilityRegistry(),
                          clock=lambda: datetime(2026, 9, 26, 14, 5))
    for question in ("Quel jour sommes-nous ?", "Jarvis, on est quel jour ?", "Quelle est la date d'aujourd'hui ?"):
        route = router.route(question)
        assert route.name == "date" and "samedi 26 septembre" in route.reply and "2026" not in route.reply, (question, route)
    route = router.route("Quelle heure est-il ?")
    assert route.name == "time" and "14 heures 5" in route.reply


def test_spoken_time_and_date_edge_cases():
    from datetime import datetime

    from jarvis.personality import spoken_date, spoken_time

    assert spoken_time(datetime(2026, 1, 1, 0, 0)) == "minuit"
    assert spoken_time(datetime(2026, 1, 1, 12, 30)) == "midi 30"
    assert spoken_time(datetime(2026, 1, 1, 1, 0)) == "1 heure"
    assert spoken_date(datetime(2026, 10, 1)) == "jeudi 1er octobre"


def test_thanks_gets_a_polite_reply_and_goes_back_to_sleep():
    r = router()
    for text in ("Merci", "Merci Jarvis", "Merci, Jarvis !", "Merci beaucoup Jarvis."):
        route = r.route(text)
        assert route.name == "thanks" and route.end_conversation, (text, route)
        assert route.reply in ("Je vous en prie, monsieur.", "Avec plaisir, monsieur.", "Tout le plaisir est pour moi.",
                               "À votre service, monsieur."), route.reply


def test_user_name_is_never_spoken():
    r = router()
    for text in ("Qui est Jules ?", "Qui suis-je ?"):
        route = r.route(text)
        assert route.name == "who_is_user" and "jules" not in route.reply.lower(), route
    for intent in PERSONALITY.intents:
        assert all("{user_name}" not in response for response in intent.responses), intent.name
    prompt = r.system_prompt()
    assert "Jules" not in prompt and "jamais son prénom" in prompt
    f = r.reply_filter(user_text="Je m'appelle Jules.")
    assert [f.accept("Enchanté, Jules."), f.accept("Jules, c'est un joli prénom.")] == ["Enchanté.", "C'est un joli prénom."]
    assert r.reply_filter().accept("Jules.") is None


def test_llm_replies_stay_short_unless_details_are_asked():
    from jarvis.streaming import sentences_from_llm

    text = ("La capitale est Paris. C'est la plus grande ville. Elle compte deux millions d'habitants. "
            "La tour Eiffel date de 1889. Elle mesure trois cents mètres. ")
    short = list(sentences_from_llm(iter([text]), router().reply_filter(user_text="Quelle est la capitale ?"), {}))
    assert short == ["La capitale est Paris.", "C'est la plus grande ville."]
    long = list(sentences_from_llm(iter([text]), router().reply_filter(user_text="Explique-moi Paris."), {}))
    assert len(long) > 3 and long[-1].endswith("trois cents mètres.")


def test_system_prompt_keeps_the_llm_focused_on_the_request():
    prompt = router().system_prompt()
    assert "Répondez uniquement à ce qu'il vient de dire" in prompt and "Pas de digression" in prompt


def test_thanks_survive_stt_misspellings_of_jarvis():
    r = router()
    for heard in ("Merci Jervis.", "Merci, Gervis.", "Merci beaucoup, Charvis.", "Ok, merci, Jervis.",
                  "Super, merci Jervis.", "Merci Gervis, c'est parfait.", "Merci pour l'info-gervis.", "Merci Jarvis."):
        route = r.route(heard)
        assert route.name == "thanks" and route.end_conversation, (heard, route)
    assert r.route("Merci de me dire la capitale de l'Italie").source == "llm"
