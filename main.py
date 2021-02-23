import os
import json
import datetime
import traceback
import html

from flask import escape
import telegram
from telegram.ext import CommandHandler, MessageHandler
from telegram.ext import Dispatcher, Filters
from telegram.error import (TelegramError, Unauthorized, BadRequest,
                            TimedOut, ChatMigrated, NetworkError)

from google.cloud import secretmanager
import google.cloud.logging
from google.cloud.logging.handlers import CloudLoggingHandler
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
from firebase_admin import exceptions

import logging

# Define global values
PARSE_MODE = 'HTML'
GEO_COLLECTION = u'locations'
BENCH_COLLECTION = u'benches'
ERROR_CHAT_SECRET = 'error_chat_id'

# Use google logging module with the standard python logger
# https://googleapis.dev/python/logging/latest/stdlib-usage.html
google_logging_client = google.cloud.logging.Client()
google_logging_handler = google_logging_client.get_default_handler()

my_logger = logging.getLogger('mbs_bench_logger')
my_logger.setLevel(os.getenv('LOG_SEVERITY'))
my_logger.addHandler(google_logging_handler)


# Initialize Firestore DB once at the beginning to avoid multiple initialization errors
# according to this
# https://stackoverflow.com/questions/65161914/calling-firestore-from-python-cloud-function-and-app-initialization
firebase_credentials = credentials.Certificate('credentials.json')
firebase_admin.initialize_app(firebase_credentials)

FIRESTORE_CLIENT = firestore.client()


def mbs_bench_bot(request):
    '''
        HTTP Cloud Function (original docstring from Google Example :-)).

    Args:
        request (flask.Request): The request object.
        <https://flask.palletsprojects.com/en/1.1.x/api/#incoming-request-data>

    Returns:
        The response text, or any set of values that can be turned into a
        Response object using `make_response`
        <https://flask.palletsprojects.com/en/1.1.x/api/#flask.make_response>.
    '''

    my_logger.info('Function execution starts now')

    bot_token = os.getenv('BOT_TOKEN')


    my_bot = telegram.Bot(bot_token)
    my_dispatcher = Dispatcher(my_bot, None, workers=0, use_context=True)

    my_logger.info('Adding the start handler')
    start_handler = telegram.ext.CommandHandler('start', handler_start)
    my_logger.debug(str(start_handler))
    my_dispatcher.add_handler(start_handler)

    my_logger.debug('Adding the echo handler')
    echo_handler = telegram.ext.MessageHandler(
        telegram.ext.Filters.chat_type.groups, handler_echo)
    my_logger.debug(str(echo_handler))
    my_dispatcher.add_handler(echo_handler)

    my_logger.debug('Adding the error handler')
    my_dispatcher.add_error_handler(handler_error)

    request_json = request.get_json(silent=True)

    the_update = telegram.Update.de_json(request_json, my_bot)
    my_dispatcher.process_update(the_update)

    return f'HTTP/1.0 200 OK'


def handler_start(the_update: telegram.update, the_context: telegram.ext.CallbackQueryHandler) -> None:

    my_logger.info('Executing start handler')

    message_text = read_text_from_file(
        'start_message', get_language_code(the_update.effective_user))
    my_logger.debug(f'Message: "{message_text}"')

    the_context.bot.send_message(
        chat_id=the_update.effective_chat.id,
        text=escape_text(message_text),
        parse_mode=PARSE_MODE)


def handler_echo(the_update: telegram.update, the_context: telegram.ext.CallbackQueryHandler) -> None:

    if telegram.utils.helpers.effective_message_type(the_update) == 'new_chat_members':

        my_logger.info('New members are added to the group')
        new_chat_members(the_update, the_context)

    elif telegram.utils.helpers.effective_message_type(the_update) == 'location':

        my_logger.info('A location was send to the group')
        save_bench_location(the_update.effective_chat.id,
                            the_update.message.location, the_update.message.from_user.id)

    elif the_update.message.text is not None:

        if the_update.message.text[0] == '/':
            my_logger.info('A command was send to the bot')
            process_own_commands(the_update, the_context)


def handler_error(the_update: telegram.update, the_context: telegram.ext.CallbackContext) -> None:
    '''
        Log the error and send a telegram message to notify the developer.
        Function copied from example:
        https://github.com/python-telegram-bot/python-telegram-bot/blob/master/examples/errorhandlerbot.py
    '''

    # Log the error before we do anything else, so we can see it even if something breaks.
    my_logger.error(msg="Exception while handling an update:",
                    exc_info=the_context.error)

    # traceback.format_exception returns the usual python message about an exception, but as a
    # list of strings rather than a single string, so we have to join them together.
    tb_list = traceback.format_exception(
        None, the_context.error, the_context.error.__traceback__)
    tb_string = ''.join(tb_list)

    # Build the message with some markup and additional information about what happened.
    # You might need to add some logic to deal with messages longer than the 4096 character limit.
    message_text = (
        f'An exception was raised while handling an update\n'
        f'<pre>update = {html.escape(json.dumps(the_update.to_dict(), indent=2, ensure_ascii=False))}'
        '</pre>\n\n'
        f'<pre>context.chat_data = {html.escape(str(the_context.chat_data))}</pre>\n\n'
        f'<pre>context.user_data = {html.escape(str(the_context.user_data))}</pre>\n\n'
        f'<pre>{html.escape(tb_string)}</pre>'
    )

    # Finally, send the message
    the_context.bot.send_message(chat_id=int(os.getenv('ERROR_CHAT_ID')), text=message_text, parse_mode=PARSE_MODE)


def new_chat_members(the_update: telegram.update, the_context: telegram.ext.CallbackContext) -> None:
    '''
        Processing of new group members. When the new member is this bot, a new bench will be initialized.
        To all other members, a welcome message will be send.

    Args:
        the_update (telegram.update)
        the_context (telegram.ext.CallbackContext)
    '''

    my_logger.info('Start processing of new group members')
    for new_member in the_update.message.new_chat_members:

        my_logger.debug(f'New member: {new_member.username}')
        if new_member.username == os.getenv('BOT_NAME'):
            my_logger.info(f'Initialize new bench')
            init_new_bench(the_update, the_context)

        else:
            my_logger.info(f'Welcome new chat members')
            welcome_new_member(new_member, the_update, the_context)


def init_new_bench(the_update: telegram.update, the_context: telegram.ext.CallbackContext) -> None:
    '''
        When the bot was added as an admin of the group, the bench will
        be added to the database and an initial message will be send to group.

    Args:
        the_update (telegram.update)
        the_context (telegram.ext.CallbackContext)
    '''

    try:
        my_logger.info('Start initializing a new bench group')
        if is_group_admin(the_update.effective_chat.get_administrators(), os.getenv('BOT_NAME')):
            my_logger.debug('Adding bench to firestore db')
            add_bench_to_db(the_update.effective_chat.title,
                            the_update.effective_chat.id)

            my_logger.info('Send initial group message')
            message_text = read_text_from_file(
                'group_pinned_message', get_language_code(the_update.effective_user))
            message = the_context.bot.send_message(
                chat_id=the_update.effective_chat.id,
                text=escape_text(message_text),
                parse_mode=PARSE_MODE)
            my_logger.debug(
                f'Message is send. Message ID: {message.message_id}')

            my_logger.info('Pin initial message to chat')
            the_context.bot.pin_chat_message(
                chat_id=the_update.effective_chat.id,
                message_id=message.message_id)

        else:
            my_logger.debug('Admin Bot was not configured as admin')
            message_text = read_text_from_file(
                'no_admin', get_language_code(the_update.effective_user))
            the_context.bot.send_message(
                chat_id=the_update.effective_chat.id,
                text=escape_text(message_text),
                parse_mode=PARSE_MODE)

    except TelegramError as err:
        my_logger.error(f'A Telegram error occurred: {err}')

    except Exception as err:
        my_logger.error(f'A common error occurred: {err}')


def welcome_new_member(new_member: telegram.user.User, the_update: telegram.update, the_context: telegram.ext.CallbackContext) -> None:
    '''
        Sending welcome messages to new team members

    Args:
        new_member (telegram.user.User): The object with the new group member
        the_update (telegram.update): [description]
        the_context (telegram.ext.CallbackContext): [description]
    '''

    try:

        my_logger.info(
            f'Send welcome message to new group member: {new_member.id}')
        my_logger.debug('Creating welcome message')
        message_text = f'{read_text_from_file("welcome_salutation", get_language_code(new_member))} {new_member.first_name}, \n{read_text_from_file("welcome_text", get_language_code(new_member))}'
        my_logger.debug('Sending welcome message')
        the_context.bot.send_message(chat_id=the_update.effective_chat.id,
                                     text=escape_text(message_text),
                                     parse_mode=PARSE_MODE)

    except TelegramError as err:
        my_logger.error(f'Error sending the message: {err}')

    except Exception as err:
        my_logger.error(f'A commom error occurred: {err}')


def is_group_admin(admins: list, username: str) -> bool:
    '''
        Check, if the user is member of the group admins

    Args:
        admins (list): The list of group admins
        username (str): The username of the admin bot
    '''

    is_admin = False

    my_logger.info('Check membership in group admins')
    my_logger.debug(f'Group admins: {admins}')
    my_logger.debug(f'Username: {username}')
    for admin in admins:
        if admin.user.username == username:
            is_admin = True

    my_logger.info(f'Result: {str(is_admin)}')
    return(is_admin)


def process_own_commands(the_update: telegram.update, the_context: telegram.ext.CallbackQueryHandler) -> None:
    '''
        Group members can interact with the bot by sending commands. These "commands" are different from the ones, defined
        by the CommandHandler of the bot. Like the "official" bot commands, they must also be started with a slash (/).
        At the moment, it exists only the "position" command which returns the latest position of the bench.

    Args:
        the_update (telegram.update):
        the_context (telegram.ext.CallbackQueryHandler):
    '''

    if the_update.message.text[1:].lower() == 'position':
        process_position_command(
            chat_id=the_update.effective_chat.id,
            the_bot=the_context.bot,
            user_id=the_update.effective_user.id)


def process_position_command(chat_id: int, the_bot: telegram.bot, user_id: int) -> None:
    '''
        Runs the command to show the current bench position

    Args:
        chat_id (int): [description]
        the_bot (telegram.bot): [description]
        user_id (int): The id of the user which requests the position
    '''

    try:
        my_logger.info('Get the latest bench location')
        latest_location = get_latest_location(chat_id)

        date_format = '%d.%m.%Y'
        time_format = '%H:%M'

        message_text = read_text_from_file('last_location', get_language_code(
            the_bot.get_chat_member(chat_id=chat_id, user_id=user_id).user))

        replacements = {
            'username': the_bot.get_chat_member(chat_id=chat_id, user_id=int(latest_location["user_id"])).user.first_name,
            'date': latest_location["date"].strftime(date_format),
            'time': latest_location["date"].strftime(time_format)
        }

        message_text = replace_variables_in_text(message_text, replacements)

        the_bot.send_message(
            chat_id=chat_id,
            text=escape_text(message_text),
            parse_mode=PARSE_MODE)

        the_bot.send_location(
            chat_id=chat_id,
            location=latest_location['location'])

    except TelegramError as err:
        my_logger.error(f'A Telegram error occurred: {err}')

    except Exception as err:
        my_logger.error(f'A common error occurred: {err}')


def add_bench_to_db(bench_display_name: str, chat_id: int) -> None:
    '''
        Add a new bench to the firestore database

    Args:
        bench_display_name (str): The display name of the bench
        chat_id (int): The ID of the current chat
    Return:
        The id of the new bench in the firestore database
    '''

    my_logger.info('Start adding new bench to db')

    try:

        my_logger.debug(f'Creating dataset')
        data = {
            u'display_name': bench_display_name
        }
        my_logger.debug(f'Dataset: {data}')

        my_logger.debug(f'Adding data to database')
        new_bench = FIRESTORE_CLIENT.collection(
            BENCH_COLLECTION).document(str(chat_id)).set(data)
        my_logger.info(
            f'New bench successfull added. New Id is: {new_bench[1].id}')

    except exceptions.FirebaseError as e:
        my_logger.error(f'A Firebase error occurred: {e}')

    except Exception as err:
        my_logger.error(f'An error occurred: {err}')


def get_secret(project_id: str, secret_id: str) -> str:
    '''
        The function reads a secret from Google Secret Manager. To make this working,
        Google Secret Manager must be properly configured for the project (see prepare_gcp.md).

    Args:
        project_id (str): The ID of the Google Cloud project
        secret_id (str): The ID of the secret

    Returns:
        str: The value of secret
    '''

    my_logger.info('Get secrets from Secret Manager')

    # Create the Secret Manager client.
    client = secretmanager.SecretManagerServiceClient()

    # Build the resource name of the secret version.
    secret_name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    my_logger.info(f'Name of the secret: {secret_name}')

    # Access the secret version.
    response = client.access_secret_version(request={"name": secret_name})
    payload = response.payload.data.decode("UTF-8")

    my_logger.info('Secrets received successful')
    return(payload)


def save_bench_location(bench_id: int, location: telegram.Location, user_id: int) -> None:
    '''
        Writes the location data, received from Telegram as new position of the bench to Firestore

    Args:
        bench_id (int): The ID of the chat used as identifier of the bench
        location (telegram.Location): Location data from the message
        name (int): The ID of the user posting the position
    '''

    try:

        data = {
            u'date': datetime.datetime.now(),
            u'user_id': str(user_id),
            u'location': {
                u'latitude': location.latitude,
                u'longitude': location.longitude
            }
        }

        FIRESTORE_CLIENT.collection(BENCH_COLLECTION).document(
            str(bench_id)).collection(GEO_COLLECTION).add(data)

    except Exception as err:
        my_logger.error(f'An error occurred: {err}')


def get_latest_location(bench_id: int) -> dict:
    '''
        Reading the latest location of a bench, given by bench_id, out of Firestore

    Args:
        bench_id (int): The ID of the chat used as identifier of the bench

    Returns:
        dict: [user_id, date, telegram.location]
    '''

    try:

        locations_ref = FIRESTORE_CLIENT.collection(BENCH_COLLECTION).document(
            str(bench_id)).collection(GEO_COLLECTION)
        query = locations_ref.order_by(
            u'date', direction=firestore.Query.DESCENDING).limit(1)
        locations = query.stream()

        for location in locations:
            data = {
                u'date': location.to_dict()['date'],
                u'user_id': location.to_dict()['user_id'],
                u'location': telegram.Location(longitude=location.to_dict()['location']['longitude'], latitude=location.to_dict()['location']['latitude'])
            }
            return(data)

    except Exception as err:
        my_logger.error(f'An error occurred: {err}')


def read_text_from_file(text_block: str, user_language: str) -> str:
    '''
        To customize the messages, which are send to the user, the message text is stored in an separate text file.
        This function reads this text file and returns the requested message text in the specified language.

    Args:
        text_block (str): Name of the text block
        user_language (str): The language of the message text in 2 char notation e. g. DE, EN

    Returns:
        str: The message text in the specified language
    '''

    try:
        my_logger.info('Read message text from file')

        text_filename = f'text.json'
        my_logger.debug(f'Filename: {text_filename}')

        my_logger.debug('Open file')
        with open(text_filename) as text_file:

            my_logger.debug('Read file')
            messages = json.load(text_file)

            my_logger.debug('Close file')
            text_file.close()

            return(messages[text_block][user_language])

    except Exception as err:
        my_logger.error(f'An error occurred: {err}')
        return('<empty string')


def replace_variables_in_text(text: str, replacements: dict) -> str:
    '''
        To make messages more "personal", the text can contain variable entries.
        At the moment, these are the values for date, time and username
        (they must be enclosed in brackets in the text to be acknowledged as variable).
        This functions replaces those variables in the given text.

    Args:
        text (str): The text containing variable entries
        replacements (dict): A dictionary with the replacements.

    Returns:
        str: The text including the replacements
    '''

    my_logger.info('Start replacing variables in the text')
    if 'date' in replacements.keys() and '[date]' in text:
        my_logger.debug(
            f'Text contains date variable, will be replaced with: {replacements["date"]}')
        text = text.replace('[date]', replacements['date'])

    if 'time' in replacements.keys() and '[time]' in text:
        my_logger.debug(
            f'Text contains time variable, will be replaced with: {replacements["time"]}')
        text = text.replace('[time]', replacements['time'])

    if 'username' in replacements.keys() and '[username]' in text:
        my_logger.debug(
            f'Text contains username variable, will be replaced with: {replacements["username"]}')
        text = text.replace('[username]', replacements['username'])

    my_logger.info('Replacement of variables finished')

    return(text)


def get_language_code(user: telegram.User) -> str:

    if user.language_code.lower() == 'de':
        return('de')

    else:
        return('en')


def escape_text(text: str) -> str:
    if PARSE_MODE == 'HTML':
        return(html.escape(text))

    else:
        return(text)
