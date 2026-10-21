from django.core.management.base import BaseCommand
from apps.accounts.management.commands.create_default_groups import Command as CreateDefaultGroupsCommand


class Command(CreateDefaultGroupsCommand):
    help = "Assign roles and permissions to existing tenants, owners, and users"
