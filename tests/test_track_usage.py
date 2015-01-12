import unittest
import json
from os.path import join, abspath, dirname
from uuid import uuid4
from flask import request, session
from api import create_app
from api.models import User, DedupeSession, Group
from api.database import init_engine, app_session, worker_session
from test_config import DEFAULT_USER, DB_CONN
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy import text
from api.track_usage import TrackUserUsage, UserSQLStorage
from tests import DedupeAPITestCase

fixtures_path = join(dirname(abspath(__file__)), 'fixtures')

class TrackUsageTest(DedupeAPITestCase):
    ''' 
    Test the track_usage module
    '''
    def test_count(self):
        assert 'a' == 'a'