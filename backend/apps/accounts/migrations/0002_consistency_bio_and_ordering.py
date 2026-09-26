"""Three consistency fixes, one of which touches existing data.

`bio` loses null=True. On a text column that flag gives two different ways
to be empty — '' and NULL — and every query afterwards has to handle both.
The rows that are already NULL have to become '' before the column can be
narrowed, which is what the first operation does; PostgreSQL refuses the
ALTER otherwise.

The two option changes add a tiebreaker to the ordering. Without one, rows
written in the same millisecond come back in whatever order the database
chooses, and a row can land on two pages of the same listing or on neither.
"""

from django.db import migrations, models


def blank_out_null_bios(apps, schema_editor):
    # apps.get_model, not a direct import: a migration must see the model as
    # it was at this point in history, not as it is today.
    Profile = apps.get_model("accounts", "Profile")
    Profile.objects.filter(bio__isnull=True).update(bio="")


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        # Data first. Reversing needs no counterpart: a nullable column
        # accepts the empty strings this leaves behind.
        migrations.RunPython(blank_out_null_bios, migrations.RunPython.noop),
        migrations.AlterModelOptions(
            name="profile",
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.AlterModelOptions(
            name="user",
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.AlterField(
            model_name="profile",
            name="bio",
            field=models.TextField(blank=True),
        ),
    ]
