"""One optional trace per Task; no changes or backfill to historical tables."""
from . import db


class TaskTrace(db.Model):
    __tablename__ = 'task_traces'
    task_id = db.Column(db.String(36), db.ForeignKey('tasks.id'), primary_key=True)
    snapshot = db.Column(db.Text, nullable=False)
    calls = db.Column(db.Text, nullable=False, default='[]')
    outputs = db.Column(db.Text, nullable=False, default='[]')
    process_id = db.Column(db.String(36), nullable=False)
