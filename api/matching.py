import os
import json
from uuid import uuid4
from flask import Flask, make_response, request, Blueprint, \
    session as flask_session, make_response, render_template, jsonify
from api.models import DedupeSession, User
from api.app_config import DOWNLOAD_FOLDER, TIME_ZONE
from api.queue import DelayedResult, redis
from api.database import app_session as db_session, engine, Base
from api.auth import csrf, check_sessions, login_required, check_roles
from api.utils.helpers import preProcess
import dedupe
from dedupe.serializer import _to_json
from cStringIO import StringIO
from sqlalchemy.exc import NoSuchTableError, ProgrammingError
from sqlalchemy import Table, text
from datetime import datetime
from hashlib import md5
from unidecode import unidecode

matching = Blueprint('matching', __name__)

dthandler = lambda obj: obj.isoformat() if isinstance(obj, datetime) else None

def validate_post(post, user_sessions):
    session_id = post.get('session_key')
    obj = post.get('object')
    r = {'status': 'ok', 'message': '', 'object': obj}
    status_code = 200
    # should probably validate if the user has access to the session
    sess = db_session.query(DedupeSession).get(session_id)
    if session_id not in user_sessions:
        r['status'] = 'error'
        r['message'] = "You don't have access to session %s" % session_id
        status_code = 401
    elif not session_id:
        r['status'] = 'error'
        r['message'] = 'Session ID is required'
        status_code = 401
    elif not obj:
        r['status'] = 'error'
        r['message'] = 'Match object is required'
        status_code = 400
    elif not sess:
        r['status'] = 'error'
        r['message'] = 'Invalid Session ID'
        status_code = 400
    return r, status_code, sess

@csrf.exempt
@matching.route('/match/', methods=['POST'])
@check_sessions()
def match():
    try:
        post = json.loads(request.data)
    except ValueError:
        r = {
            'status': 'error',
            'message': ''' 
                The content of your request should be a 
                string encoded JSON object.
            ''',
            'object': request.data,
        }
        resp = make_response(json.dumps(r), 400)
        resp.headers['Content-Type'] = 'application/json'
        return resp
    user_sessions = flask_session['user_sessions']
    r, status_code, sess = validate_post(post, user_sessions)
    if r['status'] != 'error':
        api_key = post['api_key']
        session_id = post['session_key']
        n_matches = post.get('num_matches', 5)
        obj = post['object']
        field_defs = json.loads(sess.field_defs)
        model_fields = [f['field'] for f in field_defs]
        fields = ', '.join(['r.{0}'.format(f) for f in model_fields])
        entity_table = Table('entity_{0}'.format(session_id), Base.metadata, 
            autoload=True, autoload_with=engine, keep_existing=True)
        hash_me = ';'.join([preProcess(unicode(obj[i])) for i in model_fields])
        md5_hash = md5(unidecode(hash_me)).hexdigest()
        exact_match = db_session.query(entity_table)\
            .filter(entity_table.c.source_hash == md5_hash).first()
        match_list = []
        if exact_match:
            sel = text(''' 
                  SELECT {0} 
                  FROM "raw_{1}" AS r
                  JOIN "entity_{1}" AS e
                    ON r.record_id = e.record_id
                  WHERE e.entity_id = :entity_id
                  LIMT :limit
                '''.format(fields, session_id), 
                entity_id=exact_match.entity_id, limit=n_matches)
            rows = []
            with engine.begin() as conn:
                rows = conn.execute(sel)
            for row in rows:
                d = {f: getattr(row, f) for f in model_fields}
                d['entity_id'] = exact_match.entity_id
                d['match_confidence'] = '1.0'
                match_list.append(d)
        else:
            deduper = dedupe.StaticGazetteer(StringIO(sess.gaz_settings_file))
            for k,v in obj.items():
                obj[k] = preProcess(unicode(v))
            block_keys = tuple([b[0] for b in list(deduper.blocker([('blob', obj)]))])
            sel = text('''
                  SELECT r.record_id, {1}
                  FROM "processed_{0}" as r
                  JOIN (
                    SELECT record_id
                    FROM "match_blocks_{0}"
                    WHERE block_key IN :block_keys
                  ) AS s
                  ON r.record_id = s.record_id
                '''.format(session_id, fields))
            with engine.begin() as conn:
                data_d = {int(i[0]): dict(zip(model_fields, i[1:])) \
                    for i in list(conn.execute(sel, block_keys=block_keys))}
            if data_d:
                deduper.index(data_d)
                linked = deduper.match({'blob': obj}, threshold=0, n_matches=n_matches)
                if linked:
                    ids = []
                    confs = {}
                    for l in linked[0]:
                        id_set, confidence = l
                        ids.extend([i for i in id_set if i != 'blob'])
                        confs[id_set[1]] = confidence
                    ids = tuple(set(ids))
                    sel = text(''' 
                          SELECT {0}, r.record_id, e.entity_id
                          FROM "raw_{1}" as r
                          JOIN "entity_{1}" as e
                            ON r.record_id = e.record_id
                          WHERE r.record_id IN :ids
                        '''.format(fields, session_id))
                    matches = []
                    with engine.begin() as conn:
                        matches = list(conn.execute(sel, ids=ids))
                    for match in matches:
                        m = {f: getattr(match, f) for f in model_fields}
                        m['record_id'] = getattr(match, 'record_id')
                        m['entity_id'] = getattr(match, 'entity_id')
                        # m['match_confidence'] = float(confs[str(m['entity_id'])])
                        match_list.append(m)
        r['matches'] = match_list

    resp = make_response(json.dumps(r, default=_to_json), status_code)
    resp.headers['Content-Type'] = 'application/json'
    return resp

@csrf.exempt
@matching.route('/add-entity/<session_id>/', methods=['POST'])
@check_sessions()
def add_entity(session_id):
    ''' 
    Add an entry to the entity map. 
    POST data should be a string encoded JSON object which looks like:
    
    {
        "object": {
            "city":"Macon",
            "cont_name":"Kinght & Fisher, LLP",
            "zip":"31201",
            "firstname":null,
            "employer":null,
            "address":"350 Second St",
            "record_id":3,
            "type":"Monetary",
            "occupation":null
        },
        "api_key":"6bf73c41-404e-47ae-bc2d-051e935c298e",
        "match_id": 100,
    }

    The object key should contain a mapping of fields that are in the data
    model. If the record_id field is present, an attempt will be made to look
    up the record in the raw / processed table before making the entry. If
    match_id is present, the record will be added as a member of the entity
    referenced by the id.
    '''
    r = {
        'status': 'ok',
        'message': ""
    }
    status_code = 200
    if session_id not in flask_session['user_sessions']:
        r['status'] = 'error'
        r['message'] = "You don't have access to session {0}".format(session_id)
        status_code = 401
    else:
        obj = json.loads(request.data)['object']
        record_id = obj.get('record_id')
        if record_id:
            del obj['record_id']
        match_id = json.loads(request.data).get('match_id')
        sess = db_session.query(DedupeSession).get(session_id)
        field_defs = json.loads(sess.field_defs)
        fields = {d['field']:d['type'] for d in field_defs}
        if not set(fields.keys()) == set(obj.keys()):
            r['status'] = 'error'
            r['message'] = "The fields in the object do not match the fields in the model"
            status_code = 400
        else:
            proc_table = Table('processed_{0}'.format(session_id), Base.metadata, 
                autoload=True, autoload_with=engine, keep_existing=True)
            row = db_session.query(proc_table)\
                .filter(proc_table.c.record_id == record_id)\
                .first()
            if not row:
                raw_table = Table('raw_{0}'.format(session_id), Base.metadata, 
                    autoload=True, autoload_with=engine, keep_existing=True)
                proc_ins = 'INSERT INTO "processed_{0}" (SELECT record_id, '\
                    .format(proc_table_name)
                for idx, field in enumerate(fields.keys()):
                    try:
                        field_type = field_defs[field]
                    except KeyError:
                        field_type = 'String'
                    # TODO: Need to figure out how to parse a LatLong field type
                    if field_type == 'Price':
                        col_def = 'COALESCE(CAST("{0}" AS DOUBLE PRECISION), 0.0) AS {0}'.format(field)
                    else:
                        col_def = 'CAST(TRIM(COALESCE(LOWER("{0}"), \'\')) AS VARCHAR) AS {0}'.format(field)
                    if idx < len(fields.keys()) - 1:
                        proc_ins += '{0}, '.format(col_def)
                    else:
                        proc_ins += '{0} '.format(col_def)
                else:
                    proc_ins += 'FROM "raw_{0}" WHERE record_id = :record_id)'\
                        .format(session_id)

                with engine.begin() as conn:
                    record_id = conn.execute(raw_table.insert()\
                        .returning(raw_table.c.record_id) , **obj)
                    conn.execute(text(proc_ins), record_id=record_id)
            hash_me = ';'.join([preProcess(unicode(obj[i])) for i in fields.keys()])
            md5_hash = md5(unidecode(hash_me)).hexdigest()
            entity = {
                'entity_id': unicode(uuid4()),
                'record_id': record_id,
                'source_hash': md5_hash,
                'clustered': True,
                'checked_out': False,
            }
            if match_id:
                entity['target_record_id'] = match_id
            entity_table = Table('entity_{0}'.format(session_id), Base.metadata, 
                autoload=True, autoload_with=engine, keep_existing=True)
            with engine.begin() as conn:
                conn.execute(entity_table.insert(), **entity)
    resp = make_response(json.dumps(r), status_code)
    resp.headers['Content-Type'] = 'application/json'
    return resp

@csrf.exempt
@matching.route('/train/', methods=['POST'])
@check_sessions()
def train():
    try:
        post = json.loads(request.data)
    except ValueError:
        post = json.loads(request.form.keys()[0])
    user_sessions = flask_session['user_sessions']
    r, status_code, user, sess = validate_post(post)
    if not post.get('matches'):
        r['status'] = 'error'
        r['message'] = 'List of matches is required'
        status_code = 400
    if r['status'] != 'error':
        api_key = post['api_key']
        session_id = post['session_key']
        obj = post['object']
        positive = []
        negative = []
        for match in post['matches']:
            if match['match'] is 1:
                positive.append(match)
            else:
                negative.append(match)
            for k,v in match.items():
                match[k] = preProcess(unicode(v))
            del match['match_confidence']
            del match['match']
        if len(positive) > 1:
            r['status'] = 'error'
            r['message'] = 'A maximum of 1 matching record can be sent. \
                More indicates a non-canonical dataset'
            status_code = 400
        else:
            training_data = json.loads(sess.training_data)
            if positive:
                training_data['match'].append([positive[0],obj])
            for n in negative:
                training_data['distinct'].append([n,obj])
            sess.training_data = json.dumps(training_data)
            db_session.add(sess)
            db_session.commit()
    resp = make_response(json.dumps(r))
    resp.headers['Content-Type'] = 'application/json'
    return resp

@matching.route('/get-unmatched-record/<session_id>/')
@check_sessions()
def get_unmatched(session_id):
    resp = {
        'status': 'ok',
        'message': '',
        'object': {},
    }
    status_code = 200
    if session_id not in flask_session['user_sessions']:
        resp['status'] = 'error'
        resp['message'] = "You don't have access to session '{0}'".format(session_id)
        status_code = 401
    else:
        sess = db_session.query(DedupeSession).get(session_id)
        raw_fields = [f['field'] for f in json.loads(sess.field_defs)]
        raw_fields.append('record_id')
        fields = ', '.join(['r.{0}'.format(f) for f in raw_fields])
        sel = ''' 
          SELECT {0}
          FROM "raw_{1}" as r
          LEFT JOIN "entity_{1}" as e
            ON r.record_id = e.record_id
          WHERE e.record_id IS NULL
          LIMIT 1
        '''.format(fields, session_id)
        with engine.begin() as conn:
            rows = [dict(zip(raw_fields, r)) for r in conn.execute(sel)]
        resp['object'] = rows[0]
    response = make_response(json.dumps(resp), status_code)
    response.headers['Content-Type'] = 'application/json'
    return response

@matching.route('/match-demo/<session_id>/')
@login_required
@check_roles(roles=['admin', 'reviewer'])
def match_demo(session_id):
    user = db_session.query(User).get(flask_session['user_id'])
    sess = db_session.query(DedupeSession).get(session_id)
    return render_template('match-demo.html', sess=sess, user=user)

@matching.route('/bulk-match-demo/<session_id>/', methods=['GET', 'POST'])
@login_required
def bulk_match_demo(session_id):
    user = db_session.query(User).get(flask_session['user_id'])
    sess = db_session.query(DedupeSession).get(session_id)
    context = {
        'user': user,
        'sess': sess
    }
    if request.method == 'POST':
        try:
            upload = request.files.values()[0]
        except IndexError:
            upload = None
            flash('File upload is required')
        if upload:
            context['field_defs'] = [f['field'] for f in json.loads(sess.field_defs)]
            reader = UnicodeCSVReader(upload)
            context['header'] = reader.next()
            upload.seek(0)
            flask_session['bulk_match_upload'] = upload.read()
            flask_session['bulk_match_filename'] = upload.filename
    return render_template('bulk-match.html', **context)

@csrf.exempt
@matching.route('/bulk-match/<session_id>/', methods=['POST'])
@check_sessions()
def bulk_match(session_id):
    """ 
    field_map looks like:
        {
            '<dataset_field_name>': '<uploaded_field_name>',
            '<dataset_field_name>': '<uploaded_field_name>',
        }
    """
    resp = {
        'status': 'ok',
        'message': ''
    }
    status_code = 200
    files = request.files.values()
    if not files:
        try:
            files = flask_session['bulk_match_upload']
            filename = flask_session['bulk_match_filename']
        except KeyError:
            resp['status'] = 'error'
            resp['message'] = 'File upload required'
            status_code = 400
    else:
        files = files[0].read()
        filename = files.filename
    field_map = request.form.get('field_map')
    if not field_map:
        resp['status'] = 'error'
        resp['message'] = 'field_map is required'
        status_code = 400
    if status_code is 200:
        field_map = json.loads(field_map)
        token = bulkMatchWorker.delay(
            session_id,
            files, 
            field_map, 
            filename
        )
        resp['token'] = token.key
    resp = make_response(json.dumps(resp), status_code)
    resp.headers['Content-Type'] = 'application/json'
    return resp

@matching.route('/check-bulk-match/<token>/')
@check_sessions()
def check_bulk_match(token):
    rv = DelayedResult(token)
    if rv.return_value is None:
        return jsonify(ready=False)
    redis.delete(token)
    result = rv.return_value
    if result['status'] == 'ok':
        result['result'] = '/downloads/%s' % result['result']
    resp = make_response(json.dumps(result))
    resp.headers['Content-Type'] = 'application/json'
    return resp
