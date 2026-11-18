"""Additive visitor metadata. Missing ownership always means private legacy data."""
from . import db


class Visitor(db.Model):
    __tablename__ = 'pc_visitors'
    id = db.Column(db.String(64), primary_key=True)
    created = db.Column(db.Float, nullable=False)
    expires = db.Column(db.Float, nullable=False)
    preferences = db.Column(db.Text, nullable=False, default='{}')


class ResourceOwner(db.Model):
    __tablename__ = 'pc_resource_owners'
    kind = db.Column(db.String(64), primary_key=True)
    resource_id = db.Column(db.String(64), primary_key=True)
    visitor_id = db.Column(db.String(64), nullable=False, index=True)


class VisitorRequest(db.Model):
    __tablename__ = 'pc_visitor_requests'
    visitor_id = db.Column(db.String(64), primary_key=True)
    key = db.Column(db.String(128), primary_key=True)
    fingerprint = db.Column(db.String(64), nullable=False)
    created = db.Column(db.Float, nullable=False)
    task_id = db.Column(db.String(64), nullable=True)
    state = db.Column(db.String(32), nullable=False, default='pending')
    response = db.Column(db.Text, nullable=True)
    status_code = db.Column(db.Integer, nullable=True)


class VisitorCall(db.Model):
    __tablename__ = 'pc_visitor_calls'
    call_id = db.Column(db.String(64), primary_key=True)
    visitor_id = db.Column(db.String(64), nullable=False, index=True)
    task_id = db.Column(db.String(64), nullable=True)
    role = db.Column(db.String(16), nullable=False)
    created = db.Column(db.Float, nullable=False)
    reserved = db.Column(db.Float, nullable=False)
    state = db.Column(db.String(32), nullable=False, default='reserved_unknown')
