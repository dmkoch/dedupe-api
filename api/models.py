from sqlalchemy import String, Integer, LargeBinary, ForeignKey, Boolean, \
    Column, Table, Float, DateTime, Text, BigInteger, text
from sqlalchemy.orm import relationship, backref, synonym
from api.database import Base, app_session as session
from flask_bcrypt import Bcrypt
from uuid import uuid4
from datetime import datetime, timedelta
import json
from api.app_config import TIME_ZONE

bcrypt = Bcrypt()

from sqlalchemy.types import TypeDecorator, CHAR
from sqlalchemy.dialects.postgresql import UUID
import uuid

def entity_map(name, metadata, record_id_type=BigInteger):
    table = Table(name, metadata, 
        Column('entity_id', String, index=True),
        Column('reviewer', String),
        Column('date_added', DateTime(timezone=True), 
                server_default=text('CURRENT_TIMESTAMP')),
        Column('last_update', DateTime(timezone=True)),
        Column('match_type', String),
        Column('record_id', record_id_type, index=True),
        Column('target_record_id', record_id_type),
        Column('confidence', Float(precision=50)),
        Column('source_hash', String(32)),
        Column('source', String),
        Column('clustered', Boolean, default=False),
        Column('checked_out', Boolean, default=False),
        Column('checkout_expire', DateTime),
        extend_existing=True
    )
    return table

def block_map_table(name, metadata, pk_type=Integer):
    table = Table(name, metadata,
        Column('block_key', Text, index=True),
        Column('record_id', pk_type),
        extend_existing=True
    )
    return table

def get_uuid():
    return unicode(uuid4())

class DedupeSession(Base):
    __tablename__ = 'dedupe_session'
    id = Column(String, default=get_uuid, primary_key=True)
    name = Column(String, nullable=False)
    filename = Column(String, nullable=False)
    description = Column(Text)
    training_data = Column(LargeBinary)
    settings_file = Column(LargeBinary)
    gaz_settings_file = Column(LargeBinary)
    field_defs = Column(LargeBinary)
    sample = Column(LargeBinary)
    conn_string = Column(String)
    table_name = Column(String)
    status = Column(String)
    record_count = Column(Integer)
    entity_count = Column(Integer)
    review_count = Column(Integer)
    processing = Column(Boolean, default=False)
    group_id = Column(String(36), ForeignKey('dedupe_group.id'))
    group = relationship('Group', backref=backref('sessions'))

    def __repr__(self): # pragma: no cover
        return '<DedupeSession %r >' % (self.name)
    
    def as_dict(self):
        from api.utils.helpers import STATUS_LIST
        d = {
            'id': self.id,
            'name': self.name,
            'status': self.status,
            'record_count': self.record_count,
            'entity_count': self.entity_count,
            'review_count': self.review_count,
        }
        if self.field_defs:
            d['field_defs'] = json.loads(self.field_defs)
        d['status_info'] = [i for i in STATUS_LIST if i['machine_name'] == self.status]
        return d

roles_users = Table('role_users', Base.metadata,
    Column('user_id', String(36), ForeignKey('dedupe_user.id')),
    Column('role_id', Integer, ForeignKey('dedupe_role.id'))
)

groups_users = Table('group_users', Base.metadata,
    Column('user_id', String(36), ForeignKey('dedupe_user.id')),
    Column('group_id', String(36), ForeignKey('dedupe_group.id'))
)

class Role(Base):
    __tablename__ = 'dedupe_role'
    id = Column(Integer, primary_key=True)
    name = Column(String(80), unique=True)
    description = Column(String(255))
    
    def __repr__(self): # pragma: no cover
        return '<Role %r>' % self.name

class Group(Base):
    __tablename__ = 'dedupe_group'
    id = Column(String(36), default=get_uuid, primary_key=True)
    name = Column(String(10))
    description = Column(String(255))

    def __repr__(self): # pragma: no cover
        return '<Group %r>' % self.name

class User(Base):
    __tablename__ = 'dedupe_user'
    id = Column(String(36), default=get_uuid, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    email = Column(String, nullable=False, unique=True)
    active = Column(Boolean())
    _password = Column('password', String, nullable=False)
    roles = relationship('Role', secondary=roles_users,
        backref=backref('users', lazy='dynamic'))
    groups = relationship('Group', secondary=groups_users,
        backref=backref('users', lazy='dynamic'))
    
    def __repr__(self): # pragma: no cover
        return '<User %r>' % self.name

    def _get_password(self):
        return self._password
    
    def _set_password(self, value):
        self._password = bcrypt.generate_password_hash(value)

    password = property(_get_password, _set_password)
    password = synonym('_password', descriptor=password)

    def __init__(self, name, password, email):
        self.name = name
        self.password = password
        self.email = email

    @classmethod
    def get_by_username(cls, name):
        return session.query(cls).filter(cls.name == name).first()

    @classmethod
    def check_password(cls, name, value):
        user = cls.get_by_username(name)
        if not user: # pragma: no cover
            return False
        return bcrypt.check_password_hash(user.password, value)

    def is_authenticated(self):
        return True

    def is_active(self):
        return True

    def is_anonymous(self):
        return False

    def get_id(self):
        return self.id
