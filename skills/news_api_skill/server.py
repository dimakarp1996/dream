#!/usr/bin/env python

import logging
import os
import string
from time import time
import re
import random
from collections import defaultdict

from nltk.tokenize import word_tokenize
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify
from os import getenv
import sentry_sdk
from newsapi_service import CachedRequestsAPI

from common.news import OFFER_BREAKING_NEWS, BREAKING_NEWS, OFFERED_BREAKING_NEWS_STATUS, \
    OFFERED_NEWS_DETAILS_STATUS, OPINION_REQUEST_STATUS, WHAT_TYPE_OF_NEWS, OFFER_TOPIC_SPECIFIC_NEWS, \
    OFFER_TOPIC_SPECIFIC_NEWS_STATUS, OFFERED_NEWS_TOPIC_CATEGORIES_STATUS
from common.universal_templates import COMPILE_NOT_WANT_TO_TALK_ABOUT_IT, COMPILE_SWITCH_TOPIC, if_lets_chat_about_topic
from common.utils import get_skill_outputs_from_dialog, is_yes, is_no
from common.constants import CAN_CONTINUE
from common.link import link_to

from common.metrics import setup_metrics


sentry_sdk.init(getenv('SENTRY_DSN'))
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
setup_metrics(app)

N_FACTS_TO_CHOSE = 3
ASYNC_SIZE = int(os.environ.get('ASYNC_SIZE', 6))

BANNED_UNIGRAMS = ["I", 'i', "news", "something", "anything", "me"]
NEWS_TOPICS = ["Sports", "Politics", "Economy", "Science", "Arts", "Health", "Education"]

NEWS_API_REQUESTOR = CachedRequestsAPI(renew_freq_time=10800)  # time in seconds
DEFAULT_NEWS_OFFER_CONFIDENCE = 1.
WHAT_TYPE_OF_NEWS_CONFIDENCE = 0.9
NOT_SPECIFIC_NEWS_OFFER_CONFIDENCE = 1.
DEFAULT_NEWS_DETAILS_CONFIDENCE = 1.
LINKTO_CONFIDENCE = 0.9
LINKTO_FOR_LONG_RESPONSE_CONFIDENCE = 0.7
OFFER_MORE = "Do you want to hear more?"
ASK_OPINION = "What do you think about it?"

NEWS_TEMPLATES = re.compile(r"(news|(what is|what's)( the)? new|something new)")
FALSE_NEWS_TEMPLATES = re.compile(r"(s good news|s bad news|s sad news|s awful news|s terrible news)")
TELL_MORE_NEWS_TEMPLATES = re.compile(r"(tell me more|tell me next|more news|next news|other news|learn more)")


def remove_punct_and_articles(s, lowecase=True):
    articles = ['a', 'the']
    if lowecase:
        s = s.lower()
    no_punct = ''.join([c for c in s if c not in string.punctuation])
    no_articles = ' '.join([w for w in word_tokenize(no_punct) if w.lower() not in articles])
    return no_articles


def extract_topics(curr_uttr):
    """Extract entities as topics for news request. If no entities found, extract nounphrases.

    Args:
        curr_uttr: current human utterance dictionary

    Returns:
        list of mentioned entities/nounphrases
    """
    entities = []
    for ent in curr_uttr["annotations"].get("ner", []):
        if not ent:
            continue
        ent = ent[0]
        # if not (ent["text"].lower() == "alexa" and curr_uttr["text"].lower()[:5] == "alexa") and \
        if "news" not in ent["text"].lower():
            entities.append(ent["text"].lower())
    if len(entities) == 0:
        for ent in curr_uttr["annotations"].get("cobot_nounphrases", []):
            if ent.lower() not in BANNED_UNIGRAMS and "news" not in ent.lower():
                if ent in entities:
                    pass
                else:
                    entities.append(ent)
    return entities


def news_rejection(uttr):
    if re.search(COMPILE_NOT_WANT_TO_TALK_ABOUT_IT, uttr):
        return True
    elif re.search(COMPILE_SWITCH_TOPIC, uttr):
        return True
    elif "nothing" in uttr or "none" in uttr:
        return True
    else:
        return False


def collect_topics_and_statuses(dialogs):
    topics = []
    statuses = []
    prev_news_samples = []
    for i, dialog in enumerate(dialogs):
        curr_uttr = dialog["human_utterances"][-1]

        if len(dialog["bot_utterances"]) > 0:
            prev_bot_uttr_lower = dialog["bot_utterances"][-1]["text"].lower()
        else:
            prev_bot_uttr_lower = ""

        prev_news_skill_output = get_skill_outputs_from_dialog(
            dialog["utterances"][-3:], skill_name="news_api_skill", activated=True)
        if len(prev_news_skill_output) > 0 and len(prev_news_skill_output[-1]) > 0:
            logger.info(f"News skill was prev active.")
            prev_news_skill_output = prev_news_skill_output[-1]
            prev_status = prev_news_skill_output.get("news_status", "")
            prev_topic = prev_news_skill_output.get("news_topic", "")
            prev_news = prev_news_skill_output.get("curr_news", {})
            if prev_status == OFFERED_NEWS_DETAILS_STATUS and is_yes(curr_uttr):
                logger.info(f"Detected topic for news: {prev_topic}")
                topics.append(prev_topic)
                statuses.append("details")
                prev_news_samples.append(prev_news)
            elif prev_status == OFFERED_NEWS_DETAILS_STATUS and is_no(curr_uttr):
                logger.info("User refuse to get news details")
                topics.append(prev_topic)
                statuses.append("finished")
                prev_news_samples.append(prev_news)
            elif (prev_status == OFFERED_BREAKING_NEWS_STATUS or BREAKING_NEWS.lower() in prev_bot_uttr_lower) and \
                    is_yes(curr_uttr):
                logger.info("Detected topic for news: all.")
                topics.append("all")
                statuses.append("headline")
                prev_news_samples.append(prev_news)
            elif re.search(TELL_MORE_NEWS_TEMPLATES, curr_uttr["text"].lower()):
                prev_news_skill_output = get_skill_outputs_from_dialog(
                    dialog["utterances"][-7:], skill_name="news_api_skill", activated=True)
                for prev_news_out in prev_news_skill_output:
                    if prev_news_out.get("curr_news", {}) != {}:
                        prev_news = prev_news_out.get("curr_news", {})
                logger.info(f"User requested more news. Prev news was: {prev_news}")
                topics.append(prev_topic)
                statuses.append("headline")
                prev_news_samples.append(prev_news)
            elif prev_status == OFFERED_NEWS_TOPIC_CATEGORIES_STATUS:
                if not (news_rejection(curr_uttr["text"].lower()) or is_no(curr_uttr)):
                    logger.info("User chose the topic for news")
                    if "first" in curr_uttr["text"].lower() or "any" in curr_uttr["text"].lower() or \
                            "both" in curr_uttr["text"].lower():
                        topics.append(prev_topic.split()[0])
                    elif "second" in curr_uttr["text"].lower() or "last" in curr_uttr["text"].lower():
                        topics.append(prev_topic.split()[1])
                    else:
                        entities = extract_topics(curr_uttr)
                        if len(entities) != 0:
                            topics.append(entities[-1])
                        else:
                            topics.append("all")
                    logger.info(f"Chosen topic: {topics}")
                    statuses.append("headline")
                    prev_news_samples.append(prev_news)
                else:
                    logger.info("User doesn't want to get any news")
                    topics.append("all")
                    statuses.append("finished")
                    prev_news_samples.append(prev_news)
            elif prev_status == OFFER_TOPIC_SPECIFIC_NEWS_STATUS and is_yes(curr_uttr):
                logger.info(f"User wants to listen news about {prev_topic}.")
                topics.append(prev_topic)
                statuses.append("headline")
                prev_news_samples.append(prev_news)
            else:
                logger.info("News skill was active and now can offer more news.")
                topics.append("all")
                statuses.append("finished")
                prev_news_samples.append(prev_news)
        else:
            logger.info(f"News skill was NOT active.")
            about_news = (({"News"} & set(curr_uttr["annotations"].get("cobot_topics", {}).get(
                "text", []))) or re.search(NEWS_TEMPLATES, curr_uttr["text"].lower())) and \
                not re.search(FALSE_NEWS_TEMPLATES, curr_uttr["text"].lower())
            lets_chat_about_particular_topic = if_lets_chat_about_topic(curr_uttr["text"].lower())

            if BREAKING_NEWS.lower() in prev_bot_uttr_lower and is_yes:
                # news skill was not previously active
                logger.info("Detected topic for news: all.")
                topics.append("all")
                statuses.append("headline")
                prev_news_samples.append({})
            elif about_news or lets_chat_about_particular_topic:
                # the request contains something about news
                entities = extract_topics(curr_uttr)
                logger.info(f"News request on entities: `{entities}`")
                if re.search(TELL_MORE_NEWS_TEMPLATES, curr_uttr["text"].lower()):
                    # user requestd more news.
                    # look for the last 3 turns and find last discussed news sample
                    logger.info("Tell me more news request.")
                    prev_news_skill_output = get_skill_outputs_from_dialog(
                        dialog["utterances"][-7:], skill_name="news_api_skill", activated=True)
                    if len(prev_news_skill_output) > 0 and len(prev_news_skill_output[-1]) > 0:
                        prev_news_skill_output = prev_news_skill_output[-1]
                        prev_news = prev_news_skill_output.get("curr_news", {})
                        prev_topic = prev_news_skill_output.get("news_topic", "")
                    else:
                        prev_news = {}
                        prev_topic = "all"
                    logger.info("News skill was NOT prev active. User requested more news.")
                    topics.append(prev_topic)
                    statuses.append("headline")
                    prev_news_samples.append(prev_news)
                elif len(entities) == 0:
                    # no entities or nounphrases -> no special news request, get all news
                    logger.info("News request, no entities and nounphrases.")
                    topics.append("all")
                    statuses.append("headline")
                    prev_news_samples.append({})
                else:
                    # found entities or nounphrases -> special news request,
                    # get the last mentioned entity
                    # if no named entities, get the last mentioned nounphrase
                    logger.info(f"Detected topic for news: {entities[-1]}")
                    topics.append(entities[-1])
                    statuses.append("headline")
                    prev_news_samples.append({})
            else:
                logger.info("Didn't detected news request.")
                topics.append("all")
                statuses.append("finished")
                prev_news_samples.append({})
    return topics, statuses, prev_news_samples


def link_to_other_skills(human_attr, bot_attr, curr_uttr):
    link = link_to(['movie_skill', 'book_skill', "short_story_skill"], bot_attr["used_links"])
    response = link['phrase']
    if len(curr_uttr['text'].split()) <= 5 and not re.search(FALSE_NEWS_TEMPLATES, curr_uttr['text']):
        confidence = LINKTO_CONFIDENCE
    elif re.search(FALSE_NEWS_TEMPLATES, curr_uttr['text']):
        response = ""
        confidence = 0.
    else:
        confidence = LINKTO_FOR_LONG_RESPONSE_CONFIDENCE
    if link["skill"] not in bot_attr["used_links"]:
        bot_attr["used_links"][link["skill"]] = []
    bot_attr["used_links"][link["skill"]].append(link['phrase'])
    attr = {}
    return response, confidence, human_attr, bot_attr, attr


@app.route("/respond", methods=['POST'])
def respond():
    st_time = time()
    dialogs = request.json['dialogs']
    responses = []
    confidences = []
    human_attributes = []
    bot_attributes = []
    attributes = []

    topics, statuses, prev_news_samples = collect_topics_and_statuses(dialogs)

    # run asynchronous news requests
    executor = ThreadPoolExecutor(max_workers=ASYNC_SIZE)
    for i, result in enumerate(executor.map(NEWS_API_REQUESTOR.send, topics, statuses, prev_news_samples)):
        # result is a list of articles. the first one is top rated news.
        curr_topic = topics[i]
        curr_status = statuses[i]
        logger.info(f"Composing answer for topic: {curr_topic} and status: {curr_status}.")
        logger.info(f"Result: {result}.")

        bot_attr = dialogs[i]["bot"]["attributes"]
        bot_attr["used_links"] = bot_attr.get("used_links", defaultdict(list))
        human_attr = {}
        # the only difference is that result is already is a dictionary with news.

        lets_chat_about_particular_topic = if_lets_chat_about_topic(dialogs[i]["human_utterances"][-1]["text"].lower())
        if lets_chat_about_particular_topic:
            if result and result.get("title") and result.get("description"):
                # it was a lets chat about topic and we found appropriate news
                response = OFFER_TOPIC_SPECIFIC_NEWS.replace("TOPIC", curr_topic)
                confidence = LINKTO_CONFIDENCE
                attr = {"news_status": OFFER_TOPIC_SPECIFIC_NEWS_STATUS, "news_topic": curr_topic,
                        "curr_news": result, "can_continue": CAN_CONTINUE}

                responses.append(response)
                confidences.append(confidence)
                human_attributes.append(human_attr)
                bot_attributes.append(bot_attr)
                attributes.append(attr)
                continue
            else:
                responses.append("")
                confidences.append(0.)
                human_attributes.append(human_attr)
                bot_attributes.append(bot_attr)
                attributes.append({})
                continue

        if result and result.get("title") and result.get("description"):
            logger.info("Topic: {}".format(curr_topic))
            logger.info("News found: {}".format(result))
            if curr_status == "headline":
                if len(dialogs[i]["human_utterances"]) > 0:
                    curr_uttr = dialogs[i]["human_utterances"][-1]
                else:
                    curr_uttr = {"text": ""}
                if len(dialogs[i]["bot_utterances"]) > 0:
                    prev_bot_uttr_lower = dialogs[i]["bot_utterances"][-1]["text"].lower()
                else:
                    prev_bot_uttr_lower = ""

                if BREAKING_NEWS.lower() in prev_bot_uttr_lower and is_yes(curr_uttr):
                    response = f"Here it is: {result['title']}.. {OFFER_MORE}"
                    confidence = DEFAULT_NEWS_OFFER_CONFIDENCE
                    attr = {"news_status": OFFERED_NEWS_DETAILS_STATUS, "news_topic": curr_topic,
                            "curr_news": result, "can_continue": CAN_CONTINUE}
                elif curr_topic == "all":
                    prev_news_skill_output = get_skill_outputs_from_dialog(
                        dialogs[i]["utterances"][-3:], skill_name="news_api_skill", activated=True)
                    if len(prev_news_skill_output) > 0 and \
                            prev_news_skill_output[-1].get("news_status", "") == OFFERED_NEWS_TOPIC_CATEGORIES_STATUS:
                        # topic was not detected
                        response = ""
                        confidence = 0.
                        attr = {}
                    else:
                        response = f"Here is one of the latest news that I found: {result['title']}.. {OFFER_MORE}"
                        confidence = DEFAULT_NEWS_OFFER_CONFIDENCE
                        attr = {"news_status": OFFERED_NEWS_DETAILS_STATUS, "news_topic": curr_topic,
                                "curr_news": result, "can_continue": CAN_CONTINUE}
                else:
                    response = f"Here is one of the latest news on topic {curr_topic}: {result['title']}.. {OFFER_MORE}"
                    confidence = DEFAULT_NEWS_OFFER_CONFIDENCE
                    attr = {"news_status": OFFERED_NEWS_DETAILS_STATUS, "news_topic": curr_topic,
                            "curr_news": result, "can_continue": CAN_CONTINUE}
            elif curr_status == "details":
                prev_news_skill_output = get_skill_outputs_from_dialog(
                    dialogs[i]["utterances"][-3:], skill_name="news_api_skill", activated=True)
                if len(prev_news_skill_output) > 0 and len(prev_news_skill_output[-1].get("curr_news", {})) > 0:
                    result = prev_news_skill_output[-1].get("curr_news", {})
                response = f"In details: {result['description']}. {ASK_OPINION}"
                confidence = DEFAULT_NEWS_DETAILS_CONFIDENCE
                attr = {"news_status": OPINION_REQUEST_STATUS, "news_topic": curr_topic, "curr_news": result,
                        "can_continue": CAN_CONTINUE}
            else:
                prev_news_skill_output = get_skill_outputs_from_dialog(
                    dialogs[i]["utterances"][-3:], skill_name="news_api_skill", activated=True)
                curr_uttr = dialogs[i]["human_utterances"][-1]
                # status finished is here
                if len(prev_news_skill_output) > 0 and prev_news_skill_output[-1].get(
                        "news_status", "") not in [OFFERED_NEWS_DETAILS_STATUS, OFFERED_NEWS_TOPIC_CATEGORIES_STATUS]:
                    result = prev_news_skill_output[-1].get("curr_news", {})
                    # try to offer more news
                    topics_list = NEWS_TOPICS[:]
                    random.shuffle(topics_list)
                    offered_topics = []
                    for topic in topics_list:
                        curr_topic_result = NEWS_API_REQUESTOR.send(topic=topic, status="finished",
                                                                    prev_news=prev_news_samples[i])
                        if len(curr_topic_result) > 0:
                            offered_topics.append(topic)
                            logger.info("Topic: {}".format(topic))
                            logger.info("Result: {}".format(curr_topic_result))
                        if len(offered_topics) == 2:
                            break
                    if len(offered_topics) == 2:
                        # two topics with result news were found
                        response = f"{random.choice(WHAT_TYPE_OF_NEWS)} " \
                                   f"{offered_topics[0]} or {offered_topics[1].lower()}?"
                        confidence = WHAT_TYPE_OF_NEWS_CONFIDENCE
                        attr = {"news_status": OFFERED_NEWS_TOPIC_CATEGORIES_STATUS, "can_continue": CAN_CONTINUE,
                                "news_topic": " ".join(offered_topics), "curr_news": result}
                    else:
                        # can't find enough topics for the user to offer
                        response, confidence, human_attr, bot_attr, attr = link_to_other_skills(
                            human_attr, bot_attr, curr_uttr)
                else:
                    # news was offered previously but the user refuse to get it
                    # or false news request was detected
                    response, confidence, human_attr, bot_attr, attr = link_to_other_skills(
                        human_attr, bot_attr, curr_uttr)

        else:
            # no found news
            logger.info("No particular news found.")

            if curr_topic != "all" and len(NEWS_API_REQUESTOR.cached.get("all", [])) > 0 and \
                    len(NEWS_API_REQUESTOR.cached["all"][0].get("title", "")) > 0:
                logger.info("Offer latest news.")
                response = f"Sorry, I could not find some specific news. {OFFER_BREAKING_NEWS}"
                confidence = NOT_SPECIFIC_NEWS_OFFER_CONFIDENCE
                attr = {"news_status": OFFERED_BREAKING_NEWS_STATUS, "news_topic": "all",
                        "can_continue": CAN_CONTINUE, "curr_news": prev_news_samples[i]}
            else:
                logger.info("No latest news found.")
                response = "Sorry, seems like all the news slipped my mind. Let's chat about something else. " \
                           "What do you want to talk about?"
                confidence = NOT_SPECIFIC_NEWS_OFFER_CONFIDENCE
                attr = {"news_status": OFFERED_BREAKING_NEWS_STATUS, "can_continue": CAN_CONTINUE}

        responses.append(response)
        confidences.append(confidence)
        human_attributes.append(human_attr)
        bot_attributes.append(bot_attr)
        attributes.append(attr)

    total_time = time() - st_time
    logger.info(f'news_api_skill exec time: {total_time:.3f}s')
    return jsonify(list(zip(responses, confidences, human_attributes, bot_attributes, attributes)))


@app.route("/healthz", methods=['GET'])
def healthz():
    return "OK", 200


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=3000)