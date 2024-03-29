import datetime
import time
from typing import List, Union

from requests.exceptions import ConnectionError
import requests
import tzlocal
from bottle import Bottle, request, response, DEBUG, abort
import logging
from icalendar import Calendar, vDatetime

logging.basicConfig(level=logging.DEBUG if DEBUG else logging.WARNING)

src_url = "https://cissa.webuntis.com/WebUntis/Ical.do?school=%s&id=%s&token=%s"

retry_counter = 10

cohort_class_map = {c: 1 for c in ["BOTENA", "CTA19", "CTA20", "CT19", "CT20", "DQM18", "DQM19", "DQM20",
                                   "I20", "MA19", "MA20", "N20", "OPT18", "OPT19A", "OPT19B", "OPT20A", "OPT20B",
                                   "PHY19", "PHY20", "ST18A", "ST18B", "ST19B", "ST19C", "ST19A", "ST20A", "ST20B",
                                   "PTA19", "PTA20A", "PTA20B", "CTA21", "DQM21", "I21", "MA21", "N21", "OPT21", 
                                   "PHY21", "ST21A", "ST21B", "ST21C", "PTA21A", "PTA21B" ]}
cohort_class_map.update(
    {c: 2 for c in ["DQI17", "DQI18", "DQI19", "DQI20", "DSIH18", "DSIH19A", "DSIH19B", "DSIH20A", "DSIH20B",
                    "DSIU18", "DSIU19", "DSIU20", 'FA18A', 'FA18B', 'FA18C', 'FA18D', 'FA19A', 'FA19B', 'FA19C',
                    'FA19D', 'FA20A', 'FA20B', 'FA20C', 'FA20D', "FS18A", "FS18B", "FS18C", "FS19A", "FS19B", "FS19C",
                    "FSE19D", "FS20A", "FS20B", "FS20C", "FS20D", "FSE20", "KIDM20", "SIK18", "SIK19", "W20A", "W20B",
                    "DQI21", "DSIH21", "DSIU21", "FA21A", "FA21B", "FA21C", "FA21D", 
                    "FS21A", "FS21B", "FS21C", "FS21D", "FSE21", "KIDM21", "W21A", "W21B" ]})
cohort_class_map.update({c: 3 for c in ["BLLAB18", "BLLAB19", "BLLAB20", "BOT20", "BOW20", "BTA19", "BTA20",
                                        "DQF17", "DQF18", "DQF19", "DQF20", "F19", "F20", "FAMI18", "FAMI19", "FAMI20",
                                        "ITA19", "ITA20", "IV19", "IV20", "LAB18", "LAB19", "LAB20",
                                        "BLLAB21", "BOT21", "BOW21", "BTA21", "DQF21", "F21", "FAMI21", "ITA21", 
                                        "IV21", "LAB21" ]})
cohort_minutes_map = {1: -15, 2: 0, 3: +15}

tz = tzlocal.get_localzone()
encoding = "utf-8"
app = Bottle()


def sanitize(item: Union[List[bytes], bytes, str]) -> str:
    """ contert list to string or return string

        remove duplicates and join, use utf-8
    """
    if isinstance(item, list):
        old_element = None
        result = []
        for element in item:
            if element != old_element:
                result += [element.decode(encoding)]
                old_element = element
        return " ".join(result)  # remove dupes and convert to string
    else:
        if isinstance(item, str):
            return item
        else:
            return item.decode(encoding)

def strftime(dto):
    """convert datetime object to localized string format"""
    return dto.astimezone(tz).strftime("%a, %d %b %Y %H:%M:%S")

def corrected_events(cal_in: Calendar):
    ''' build event summary and remove unwanted fields (in place) and collect events in list '''
    events = []
    for event in cal_in.subcomponents:
        if 'description' in event:
            description = sanitize(event.decoded('description'))
            logging.debug("description: %s" % description)
            classname = description.split()[0]
        else:  # tritt bei Hofpausen auf (FIXME: vernuenftige Erkennung?)
            classname = "-"
        location = sanitize(event.decoded('location', 'unbekannt'))
        subject = sanitize(event.decoded('summary', 'unbekannt'))
        event.classname = classname  # FIXME: too dirty

        try:
            del event['description']
            del event['location']
        except KeyError:
            pass  # ignore
        event['summary'] = "%s %s (%s)" % (classname, subject, location)
        logging.debug(f"resulting summary: {event['summary']}")
        events.append(event)
    return events


def join_events(events):
    """ join two consecutive events with fuzzy search """
    lastevents = []
    result = []
    for event in sorted(events, key=lambda x: x.decoded('DTSTART')):
        correction_applied = False
        for lastevent in lastevents:
            if lastevent.decoded('DTEND') == event.decoded('DTSTART') \
                    and lastevent['SUMMARY'] == event['SUMMARY']:
                lastevent['DTEND'] = event['DTEND']
                correction_applied = True
                break

        if not correction_applied:  # else throw away event
            result.append(event)
            lastevents = lastevents[-4:] + [event]  # limit last events

    return result


def get_cohort_offset(classname, dtstamp):
    """ map class string to offset in minutes: -15, 0, +15"""
    offset = 0  # default
    if classname not in ("None", "-") and classname not in cohort_class_map:
        if classname == "Loos": # bei Teamsitzungen
            return offset # keine Anpassungen und keine Fisimatenten
        else:
            logging.warning("no such class: %s" % classname)
    if classname in cohort_class_map:
        offset += cohort_minutes_map[cohort_class_map[classname]]
        logging.debug("add cohort offset for %s (%02d:%02d): %3d" % (classname, dtstamp.hour, dtstamp.minute, offset))

    """ shorten the first 3 breaks by 5 minutes each,
        after that the Untis schedule is off the intended original plan:
         - the break between 8th and 9th is 10 min too long
         - the break between 10th and 11th is also 10 min too long """
    for t in [(10, 0), (11, 50), (13, 40), (15, 30), (15, 30), (17, 20), (17, 20)]:
        if dtstamp.time() >= datetime.time(*t, tzinfo=tz):
            offset -= 5
            logging.debug("add offset for %s (%04d-%02d-%02d) (%02d:%02d > %02d:%02d): -5" %
                          (classname, dtstamp.year,
                           dtstamp.month,
                           dtstamp.day,
                           dtstamp.hour,
                           dtstamp.minute,
                           t[0], t[1]))

    return offset


def cohort_correced(events):
    times_fields = ('DTSTART', 'DTEND')
    cohort_end_date = datetime.date(2022,4,19)
    for event in events:
        times_orig = [event.decoded(i) for i in times_fields]
        if times_orig[1].date() < cohort_end_date:
            offset = get_cohort_offset(event.classname, times_orig[0].astimezone(tzlocal.get_localzone()))
        else:
            offset = 0
        times_cohort = [t + datetime.timedelta(minutes=offset) for t in times_orig]
        for i, t in zip(times_fields, times_cohort):
            event[i] = vDatetime(t).to_ical().decode()
        logging.debug("%s total offset: %d start: %s end: %s" % (
            event.decoded('SUMMARY'),
            offset,
            strftime(event.decoded('DTSTART')),
            strftime(event.decoded('DTEND'))))

    return events


@app.route('/untis_bottle')
def untisconv():
    school = request.query.school
    userid = request.query.id
    token = request.query.token

    logging.debug("school: %s; id: %s; token: %s" %( school, userid, token))
    if not(all([school, userid, token])):
        abort(510, "invalid query")

    # fetch from untis
    url = src_url % (school, userid, token)
    logging.debug(url)
    ret_cnt = retry_counter
    while ret_cnt > 0:
        ret_cnt -= 1 # consume one
        try:
            cal_in = Calendar.from_ical(requests.get(url).text)
            break
        except (ConnectionResetError, ConnectionError) as e:
            logging.warning(f"try {retry_counter-ret_cnt}: {e}")
            if ret_cnt == 0:
                response.status_code = 500
                return ''
        time.sleep(0.2)
    events = join_events(corrected_events(cal_in))
    # events = cohort_correced(events) # disabled

    # initialize new calendar
    cal_out = Calendar()
    for item in ['version', 'prodid', 'calscale']:
        cal_out.add(item, cal_in[item])

    for event in events:
        cal_out.add_component(event)

    response.content_type = 'text/calendar' if not DEBUG else 'text/plain'
    return cal_out.to_ical()
