# type: ignore
import dateutil.parser
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import linear_kernel

from CommonServerPython import *

HOURS_TIME_FRAME = float(demisto.args()['timeFrameHours'])
THRESHOLD = float(demisto.args()['threshold'])
TEXT_FIELDS = set(map(lambda x: x.lower(), demisto.args()['textFields'].split(',')))
IGNORE_CLOSED = demisto.args()['ignoreClosedIncidents'] == 'yes'
INCIDENT_QUERY_SIZE = int(demisto.args()['maximumNumberOfIncidents'])
INCIDENT_TEXT_FIELD = 'incident_text_for_tfidf'
MIN_TEXT_LENGTH = int(demisto.args()['minTextLength'])
MAX_CANDIDATES_IN_LIST = int(demisto.args()['maxResults'])
TIME_FIELD = demisto.args()['timeField']


def parse_datetime(datetime_str):
    return dateutil.parser.parse(datetime_str)


def get_similar_texts(text, other_texts):
    vect = TfidfVectorizer(min_df=1, stop_words='english')
    tfidf = vect.fit_transform([text] + other_texts)
    similarity_vector = linear_kernel(tfidf[0:1], tfidf).flatten()
    return similarity_vector[1:]


def get_texts_from_incident(incident, text_fields):
    texts = []
    # labels
    for label in incident.get('labels') or []:
        if label['type'].lower() in text_fields:
            texts.append(label['value'])

    # custom fields + incident fields
    custom_fields = incident.get('CustomFields') or {}
    for field_name, field_value in (custom_fields.items() + incident.items()):
        if field_name in text_fields and isinstance(field_value, basestring):
            texts.append(field_value)

    return " ".join(texts)


def add_text_to_incident(incident, text_fields):
    incident[INCIDENT_TEXT_FIELD] = get_texts_from_incident(incident, text_fields)


def get_incidents_by_time(incident_time, incident_type, incident_id, hours_time_frame, ignore_closed,
                          max_number_of_results):
    incident_time = parse_datetime(incident_time)
    max_date = incident_time + timedelta(hours=hours_time_frame)
    min_date = incident_time - timedelta(hours=hours_time_frame)
    query = '{0}:>="{1}" and {0}:<="{2}" and type:"{3}"'.format(TIME_FIELD, min_date.isoformat(), max_date.isoformat(),
                                                                incident_type)

    if ignore_closed:
        query += " and -closed:*"

    if incident_id:
        query += ' and -id:%s' % incident_id

    res = demisto.executeCommand("getIncidents",
                                 {'query': query,
                                  'size': max_number_of_results, 'sort': '%s.desc' % TIME_FIELD})

    if res[0]['Type'] == entryTypes['error']:
        raise Exception(str(res[0]['Contents']))

    incident_list = res[0]['Contents']['data']
    return incident_list or []


def incident_to_record(incident):
    def parse_time(date_time_str):
        try:
            if date_time_str.find('.') > 0:
                date_time_str = date_time_str[:date_time_str.find('.')]
            if date_time_str.find('+') > 0:
                date_time_str = date_time_str[:date_time_str.find('+')]
            return date_time_str.replace('T', ' ')
        except Exception:
            return date_time_str

    occured_time = parse_time(incident[TIME_FIELD])
    return {'id': "[%s](#/Details/%s)" % (incident['id'], incident['id']),
            'rawId': incident['id'],
            'name': incident['name'],
            'closedTime': parse_time(incident['closed']) if incident['closed'] != "0001-01-01T00:00:00Z" else "",
            'Time': occured_time,
            'similarity': "{0:.2f}".format(incident['similarity'])
            }


incident = demisto.incidents()[0]
incident_text = get_texts_from_incident(incident, TEXT_FIELDS)
if len(incident_text) < MIN_TEXT_LENGTH:
    demisto.results("The text is too short to compare - minimum of %d chars required" % MIN_TEXT_LENGTH)
    sys.exit(0)

# get initial candidates list
candidates = get_incidents_by_time(incident[TIME_FIELD], incident['type'], incident['id'], HOURS_TIME_FRAME,
                                   IGNORE_CLOSED, INCIDENT_QUERY_SIZE)

# filter candidates with minimum length constraint
map(lambda x: add_text_to_incident(x, TEXT_FIELDS), candidates)
candidates = [x for x in candidates if len(x.get(INCIDENT_TEXT_FIELD, 0)) >= MIN_TEXT_LENGTH]

# compare candidates to the orginial incident using TF-IDF
similarity_vector = get_similar_texts(incident_text, map(lambda x: x[INCIDENT_TEXT_FIELD], candidates))
similar_incidents = []
for (i, similarity) in enumerate(similarity_vector):
    candidates[i]['similarity'] = similarity
    if similarity >= THRESHOLD:
        similar_incidents.append(candidates[i])

# update context
if len(similar_incidents or []) > 0:
    similar_incidents_rows = map(incident_to_record, similar_incidents)
    similar_incidents_rows = sorted(similar_incidents_rows, key=lambda x: x['Time'])
    context = {
        'similarIncidentList': similar_incidents_rows[:MAX_CANDIDATES_IN_LIST],
        'similarIncident': similar_incidents_rows[0],
        'isSimilarIncidentFound': True
    }
    markdown_result = tableToMarkdown("Similar incidents",
                                      similar_incidents_rows,
                                      headers=['id', 'name', 'closedTime', 'Time', 'similarity'])
    demisto.results({'ContentsFormat': formats['markdown'],
                     'Type': entryTypes['note'],
                     'Contents': markdown_result,
                     'EntryContext': context})
else:
    context = {
        'isSimilarIncidentFound': False
    }
    demisto.results({'ContentsFormat': formats['markdown'],
                     'Type': entryTypes['note'],
                     'Contents': 'No similar incidents has been found',
                     'EntryContext': context})
