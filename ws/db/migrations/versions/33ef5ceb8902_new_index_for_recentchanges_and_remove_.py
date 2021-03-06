"""new index for recentchanges and remove page_random

Revision ID: 33ef5ceb8902
Revises: 5824cd5ed16d
Create Date: 2017-12-28 21:37:15.640099

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '33ef5ceb8902'
down_revision = '5824cd5ed16d'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index('page_random', table_name='page')
    op.drop_column('page', 'page_random')
    op.create_index('rc_name_type_patrolled_timestamp', 'recentchanges', ['rc_namespace', 'rc_type', 'rc_patrolled', 'rc_timestamp'], unique=False)
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index('rc_name_type_patrolled_timestamp', table_name='recentchanges')
    op.add_column('page', sa.Column('page_random', postgresql.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=False))
    op.create_index('page_random', 'page', ['page_random'], unique=False)
    # ### end Alembic commands ###
