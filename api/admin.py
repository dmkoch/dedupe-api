from flask import Blueprint, request, session as flask_session, \
    render_template, make_response, flash, redirect, url_for, current_app
from api.database import app_session as db_session, Base
from api.models import User, Role, DedupeSession, Group
from api.auth import login_required, check_roles, check_sessions
from api.utils.helpers import preProcess
from api.utils.delayed_tasks import cleanupTables
from flask_wtf import Form
from wtforms import TextField, PasswordField
from wtforms.ext.sqlalchemy.fields import QuerySelectMultipleField
from wtforms.validators import DataRequired, Email
from sqlalchemy import Table, and_
from sqlalchemy.sql import select
from sqlalchemy.exc import NoSuchTableError, ProgrammingError
from itertools import groupby
from operator import itemgetter
import json
from cPickle import loads
from dedupe.convenience import canonicalize
from csvkit.unicsv import UnicodeCSVReader
import dedupe
from cStringIO import StringIO
from datetime import datetime

admin = Blueprint('admin', __name__)

def role_choices():
    return db_session.query(Role).all()

def group_choices():
    return db_session.query(Group).all()

class AddUserForm(Form):
    name = TextField('name', validators=[DataRequired()])
    email = TextField('email', validators=[DataRequired(), Email()])
    roles = QuerySelectMultipleField('roles', query_factory=role_choices, 
                                validators=[DataRequired()])
    groups = QuerySelectMultipleField('groups', query_factory=group_choices, 
                                validators=[DataRequired()])
    password = PasswordField('password', validators=[DataRequired()])

    def __init__(self, *args, **kwargs):
        Form.__init__(self, *args, **kwargs)

    def validate(self):
        rv = Form.validate(self)
        if not rv:
            return False

        existing_name = db_session.query(User)\
            .filter(User.name == self.name.data).first()
        if existing_name:
            self.name.errors.append('Name is already registered')
            return False

        existing_email = db_session.query(User)\
            .filter(User.email == self.email.data).first()
        if existing_email:
            self.email.errors.append('Email address is already registered')
            return False
        
        return True

@admin.route('/')
@login_required
@check_roles(roles=['admin', 'reviewer'])
def index():
    user = db_session.query(User).get(flask_session['user_id'])
    roles = [r.name for r in user.roles]
    return render_template('index.html', user=user, roles=roles)

@admin.route('/add-user/', methods=['GET', 'POST'])
@login_required
@check_roles(roles=['admin'])
def add_user():
    form = AddUserForm()
    user = db_session.query(User).get(flask_session['user_id'])
    if form.validate_on_submit():
        user_info = {
            'name': form.name.data,
            'email': form.email.data,
            'password': form.password.data,
        }
        user = User(**user_info)
        db_session.add(user)
        db_session.commit()
        user.roles = form.roles.data
        user.groups = form.groups.data
        db_session.add(user)
        db_session.commit()
        flash('User %s added' % user.name)
        return redirect(url_for('admin.user_list'))
    return render_template('add_user.html', form=form, user=user)

@admin.route('/user-list/')
@login_required
@check_roles(roles=['admin'])
def user_list():
    users = db_session.query(User).all()
    user = db_session.query(User).get(flask_session['user_id'])
    return render_template('user_list.html', users=users, user=user)

@admin.route('/session-admin/<session_id>/')
@login_required
@check_sessions()
def session_admin(session_id):
    if session_id not in flask_session['user_sessions']:
        flash("You don't have access to session {0}".format(session_id))
        return redirect(url_for('admin.index'))
    else:
        user = db_session.query(User).get(flask_session['user_id'])
        sess = db_session.query(DedupeSession).get(session_id)
        predicates = None
        session_info = {}
        training_data = None
        status_info = sess.as_dict()['status_info']
        if sess.field_defs:
            field_defs = json.loads(sess.field_defs)
            for fd in field_defs:
                try:
                    session_info[fd['field']]['types'].append(fd['type'])
                    session_info[fd['field']]['has_missing'] = fd.get('has_missing', '')
                    session_info[fd['field']]['children'] = []
                except KeyError:
                    session_info[fd['field']] = {
                                                  'types': [fd['type']],
                                                  'has_missing': fd.get('has_missing', ''),
                                                }
                    session_info[fd['field']]['children'] = []
        if sess.settings_file:
            dd = dedupe.StaticDedupe(StringIO(sess.settings_file))
            for field in dd.data_model.primary_fields:
                name, ftype = field.field, field.type
                if ftype in ['Categorical', 'Address', 'Set']:
                    children = []
                    for f in field.higher_vars:
                        children.append((f.name, f.type, f.has_missing, f.weight,) )
                    session_info[name]['children'] = children
                try:
                    session_info[name]['learned_weight'] = field.weight
                except KeyError: # pragma: no cover
                    session_info[name] = {'learned_weight': field.weight}
            predicates = dd.predicates
        if sess.training_data:
            td = json.loads(sess.training_data)
            training_data = {'distinct': [], 'match': []}
            for left, right in td['distinct']:
                keys = left.keys()
                pair = []
                for key in keys:
                    d = {
                        'field': key,
                        'left': left[key],
                        'right': right[key]
                    }
                    pair.append(d)
                training_data['distinct'].append(pair)
            for left, right in td['match']:
                keys = left.keys()
                pair = []
                for key in keys:
                    d = {
                        'field': key,
                        'left': left[key],
                        'right': right[key]
                    }
                    pair.append(d)
                training_data['match'].append(pair)
    return render_template('session-admin.html', 
                            dd_session=sess, 
                            session_info=session_info, 
                            predicates=predicates,
                            training_data=training_data,
                            user=user,
                            status_info=status_info)

@admin.route('/training-data/<session_id>/')
@check_sessions()
def training_data(session_id):
    user_sessions = flask_session['user_sessions']
    if session_id not in user_sessions:
        r = {
            'status': 'error', 
            'message': "You don't have access to session %s" % session_id
        }
        resp = make_response(json.dumps(r), 401)
        resp.headers['Content-Type'] = 'application/json'
    else:
        data = db_session.query(DedupeSession).get(session_id)
        training_data = data.training_data
        resp = make_response(training_data, 200)
        resp.headers['Content-Type'] = 'text/plain'
        resp.headers['Content-Disposition'] = 'attachment; filename=%s_training.json' % data.id
    return resp

@admin.route('/settings-file/<session_id>/')
@check_sessions()
def settings_file(session_id):
    user_sessions = flask_session['user_sessions']
    if session_id not in user_sessions:
        r = {
            'status': 'error', 
            'message': "You don't have access to session %s" % session_id
        }
        resp = make_response(json.dumps(r), 401)
        resp.headers['Content-Type'] = 'application/json'
    else:
        data = db_session.query(DedupeSession).get(session_id)
        settings_file = data.settings_file
        resp = make_response(settings_file, 200)
        resp.headers['Content-Disposition'] = 'attachment; filename=%s.dedupe_settings' % data.id
    return resp

@admin.route('/field-definitions/<session_id>/')
@check_sessions()
def field_definitions(session_id):
    user_sessions = flask_session['user_sessions']
    if session_id not in user_sessions:
        r = {
            'status': 'error', 
            'message': "You don't have access to session %s" % session_id
        }
        resp = make_response(json.dumps(r), 401)
        resp.headers['Content-Type'] = 'application/json'
    else:
        data = db_session.query(DedupeSession).get(session_id)
        field_defs = data.field_defs
        resp = make_response(field_defs, 200)
        resp.headers['Content-Type'] = 'application/json'
    return resp

@admin.route('/delete-data-model/<session_id>/')
@check_sessions()
def delete_data_model(session_id):
    user_sessions = flask_session['user_sessions']
    if session_id not in user_sessions:
        resp = {
            'status': 'error', 
            'message': "You don't have access to session %s" % session_id
        }
        status_code = 401
    else:
        sess = db_session.query(DedupeSession).get(session_id)
        sess.field_defs = None
        sess.training_data = None
        sess.sample = None
        sess.status = 'session initialized'
        db_session.add(sess)
        db_session.commit()
        tables = [
            'entity_{0}',
            'block_{0}',
            'plural_block_{0}',
            'covered_{0}',
            'plural_key_{0}',
            'small_cov_{0}',
        ]
        engine = db_session.bind
        for table in tables: # pragma: no cover
            try:
                data_table = Table(table.format(session_id), 
                    Base.metadata, autoload=True, autoload_with=engine)
                data_table.drop(engine)
            except NoSuchTableError:
                pass
            except ProgrammingError:
                pass
        resp = {
            'status': 'ok',
            'message': 'Data model for session {0} deleted'.format(session_id)
        }
        status_code = 200
    resp = make_response(json.dumps(resp), status_code)
    resp.headers['Content-Type'] = 'application/json'
    return resp

@admin.route('/delete-session/<session_id>/')
@check_sessions()
def delete_session(session_id):
    user_sessions = flask_session['user_sessions']
    if session_id not in user_sessions:
        r = {
            'status': 'error', 
            'message': "You don't have access to session %s" % session_id
        }
        resp = make_response(json.dumps(r), 401)
        resp.headers['Content-Type'] = 'application/json'
    else:
        data = db_session.query(DedupeSession).get(session_id)
        db_session.delete(data)
        db_session.commit()
        tables = [
            'entity_{0}',
            'entity_{0}_cr',
            'raw_{0}',
            'processed_{0}',
            'processed_{0}_cr',
            'block_{0}',
            'block_{0}_cr',
            'plural_block_{0}',
            'plural_block_{0}_cr',
            'cr_{0}',
            'covered_{0}',
            'covered_{0}_cr',
            'plural_key_{0}',
            'plural_key_{0}_cr',
            'small_cov_{0}',
            'small_cov_{0}_cr',
            'canon_{0}',
            'exact_match_{0}',
        ]
        cleanupTables.delay(session_id, tables=tables)
        resp = make_response(json.dumps({'session_id': session_id, 'status': 'ok'}))
        resp.headers['Content-Type'] = 'application/json'
    return resp

@admin.route('/session-list/')
@check_sessions()
def review():
    user_sessions = flask_session['user_sessions']
    resp = {
        'status': 'ok',
        'message': ''
    }
    status_code = 200
    sess_id = request.args.get('session_id')
    all_sessions = []
    if not sess_id:
        sessions = db_session.query(DedupeSession)\
            .filter(DedupeSession.id.in_(user_sessions))\
            .all()
        for sess in sessions:
            s = sess.as_dict()
            all_sessions.append(s)
    else:
        if sess_id in user_sessions:
            sess = db_session.query(DedupeSession).get(sess_id)
            s = sess.as_dict()
            all_sessions.append(s)
    resp['objects'] = all_sessions
    response = make_response(json.dumps(resp), status_code)
    response.headers['Content-Type'] = 'application/json'
    return response

@admin.route('/dump-entity-map/<session_id>/')
@check_sessions()
def entity_map_dump(session_id):
    user_sessions = flask_session['user_sessions']
    if session_id not in user_sessions: # pragma: no cover
        r = {
            'status': 'error', 
            'message': "You don't have access to session %s" % session_id
        }
        resp = make_response(json.dumps(r), 401)
        resp.headers['Content-Type'] = 'application/json'
        return resp
    outp = StringIO()
    copy = """ 
        COPY (
          SELECT 
            e.entity_id, 
            r.* 
          FROM \"raw_{0}\" AS r
          LEFT JOIN \"entity_{0}\" AS e
            ON r.record_id = e.record_id
          WHERE e.clustered = TRUE
          ORDER BY e.entity_id NULLS LAST
        ) TO STDOUT WITH CSV HEADER DELIMITER ','
    """.format(session_id)
    engine = db_session.bind
    conn = engine.raw_connection()
    curs = conn.cursor()
    curs.copy_expert(copy, outp)
    outp.seek(0)
    resp = make_response(outp.getvalue())
    resp.headers['Content-Type'] = 'text/csv'
    filedate = datetime.now().strftime('%Y-%m-%d')
    resp.headers['Content-Disposition'] = 'attachment; filename=entity_map_{0}.csv'.format(filedate)
    return resp
