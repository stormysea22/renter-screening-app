"""empty message

Revision ID: 5fefa27a3a32
Revises: 5d909cfb8e61
Create Date: 2025-05-02 15:21:18.921800

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5fefa27a3a32'
down_revision = '5d909cfb8e61'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('application', schema=None) as batch_op:
        batch_op.add_column(sa.Column('active', sa.Boolean(), nullable=True))

    with op.batch_alter_table('house', schema=None) as batch_op:
        batch_op.add_column(sa.Column('active', sa.Boolean(), nullable=True))

    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('active', sa.Boolean(), nullable=True))

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('active')

    with op.batch_alter_table('house', schema=None) as batch_op:
        batch_op.drop_column('active')

    with op.batch_alter_table('application', schema=None) as batch_op:
        batch_op.drop_column('active')

    # ### end Alembic commands ###
