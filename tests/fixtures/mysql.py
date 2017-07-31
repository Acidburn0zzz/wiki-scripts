#! /usr/bin/env python3

import pytest
# FIXME: this is found in the docs, but does not work
#from pytest_dbfixtures import factories
from pytest_mysql import factories
import sqlalchemy
from sqlalchemy.engine.url import make_url
from copy import copy

# FIXME: probably an Arch bug
mysql_init = "/usr/bin/mysql_install_db --basedir=/usr"

mysql_proc = factories.mysql_proc(port=(3000, 4000), params="--skip-sync-frm", logs_prefix="pytest-", init_executable=mysql_init)

user = "root"
password = ""
db_name = "wiki-scripts"
db_charset = "utf8"
db_collation = "utf8_general_ci"

@pytest.fixture(scope="session")
def sqlalchemy_connect_url(mysql_proc):
    socket = mysql_proc.socket_path
    return "mysql+pymysql://{user}:{password}@localhost/{db}?unix_socket={socket}&charset=utf8".format(
            user=user, password=password, db=db_name, socket=socket)

@pytest.fixture(scope="module")
def engine(sqlalchemy_connect_url):
    # strip database from URL until it is created
    url = copy(make_url(sqlalchemy_connect_url))
    url.database = ""

    # engine for maintenance
    e = sqlalchemy.create_engine(url)
    e.execute(
        """CREATE DATABASE `{name}`
        DEFAULT CHARACTER SET {charset}
        DEFAULT COLLATE {collation}"""
        .format(name=db_name, charset=db_charset, collation=db_collation))
    e.dispose()

    # engine for the fixture
    engine = sqlalchemy.create_engine(sqlalchemy_connect_url)
    yield engine
    engine.dispose()

    # engine for maintenance
    e = sqlalchemy.create_engine(url)
    e.execute(
        """DROP DATABASE `{name}`"""
        .format(name=db_name))
    e.dispose()
