#!/usr/bin/env python

import logging
import time
from os import getenv
import random
import pathlib
import datetime
import traceback

from flask import Flask, request, jsonify, abort
from healthcheck import HealthCheck
import sentry_sdk
from sentry_sdk.integrations.logging import ignore_logger

from common.constants import CAN_NOT_CONTINUE, CAN_CONTINUE
from common.universal_templates import is_switch_topic

from common.utils import get_skill_outputs_from_dialog
from router import run_skills as skill


ignore_logger("root")

sentry_sdk.init(getenv("SENTRY_DSN"))
DB_FILE = pathlib.Path(getenv("DB_FILE", "/tmp/game_db.json"))
MEMORY_LENGTH = 3

logging.basicConfig(format="%(asctime)s - %(pathname)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
health = HealthCheck(app, "/healthcheck")


# add your own check function to the healthcheck
def db_is_updated():
    curr_date = datetime.datetime.now()
    min_update_time = datetime.timedelta(hours=25)
    if DB_FILE.exists():
        file_modification_time = datetime.datetime.fromtimestamp(DB_FILE.lstat().st_mtime)
        data_is_expired = curr_date - min_update_time > file_modification_time
        msg = "db is expired" if data_is_expired else "db is updated"
        msg += f", last modified date of db is {file_modification_time.strftime('%m/%d/%Y, %H:%M:%S')}"
        if data_is_expired:
            sentry_sdk.capture_message(msg)
        return True, msg
    else:
        msg = "db file is not created"
        logger.error(msg)
        sentry_sdk.capture_message(msg)
        return False, msg


health.add_check(db_is_updated)


def get_agent_intents(last_utter):
    annotations = last_utter.get("annotations", {})
    agent_intents = {}
    for intent_name, intent_detector in annotations.get("intent_catcher", {}).items():
        if intent_detector.get("detected", 0) == 1:
            agent_intents[intent_name] = True

    if not agent_intents.get("topic_switching") and (
        is_switch_topic(last_utter)
        or agent_intents.get("exit")
        or agent_intents.get("stupid")
        or agent_intents.get("cant_do")
        or agent_intents.get("tell_me_a_story")
        or agent_intents.get("weather_forecast_intent")
        or agent_intents.get("what_can_you_do")
        or agent_intents.get("what_is_your_job")
        or agent_intents.get("what_is_your_name")
        or agent_intents.get("what_time")
    ):
        agent_intents["topic_switching"] = True
    return agent_intents


@app.route("/respond", methods=["POST"])
def respond():
    dialogs_batch = [None]
    try:
        st_time = time.time()
        dialogs_batch = request.json["dialogs"]
        rand_seed = request.json.get("rand_seed")
        responses = []

        for dialog in dialogs_batch:
            prev_news_outputs = get_skill_outputs_from_dialog(
                dialog["utterances"][-MEMORY_LENGTH:], "game_cooperative_skill", activated=True
            )
            is_active_last_answer = bool(prev_news_outputs)
            prev_news_outputs = (
                prev_news_outputs
                if is_active_last_answer
                else get_skill_outputs_from_dialog(dialog["utterances"], "game_cooperative_skill", activated=True)
            )
            prev_news_output = prev_news_outputs[-1] if prev_news_outputs else {}
            state = prev_news_output.get("state", {})
            if state and not is_active_last_answer:
                state["messages"] = []
            # pre_len = len(state.get("messages", []))

            last_utter = dialog["human_utterances"][-1]

            last_utter_text = last_utter["text"].lower()
            agent_intents = get_agent_intents(last_utter)

            # for tests
            if rand_seed:
                random.seed(int(rand_seed))
            response, state = skill([last_utter_text], state, agent_intents)

            # logger.info(f"state = {state}")
            logger.info(f"last_utter_text = {last_utter_text}")
            logger.info(f"response = {response}")
            text = response.get("text", "Sorry")
            confidence = 1.0 if response.get("confidence") else 0.0
            confidence *= 0.9 if "I like to talk about games." in response.get("text") else 1.0
            confidence *= 1.0 if is_active_last_answer else 0.98

            can_continue = CAN_CONTINUE if confidence else CAN_NOT_CONTINUE
            attr = {"can_continue": can_continue, "state": state, "agent_intents": agent_intents}
            responses.append((text, confidence, attr))

        total_time = time.time() - st_time
        logger.info(f"game_cooperative_skill exec time = {total_time:.3f}s")

    except Exception as exc:
        logger.error(traceback.format_exc())
        sentry_sdk.capture_exception(exc)
        abort(500, description=str(traceback.format_exc()))
    return jsonify(responses)


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=3000)