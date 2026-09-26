"""The admin, adjusted for a custom user model.

Subclassing UserAdmin rather than registering plainly is what keeps the
password field out and the password *change form* in: a plain ModelAdmin
shows the hash in a text box, and anything typed there is stored as a hash
of nothing, silently locking the account.
"""

from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import AdminUserCreationForm
from rest_framework.exceptions import ValidationError as DRFValidationError

from .models import Profile, User
from .validators.user import validate_name, validate_username


def _as_form_error(validator, value):
    """Run an API validator inside a Django form.

    The validators raise DRF's ValidationError, which a form does not
    understand — so an invalid value was a 500 instead of a field error.
    """
    try:
        return validator(value)
    except DRFValidationError as error:
        raise forms.ValidationError(error.detail[0]) from None


class UserCreateForm(AdminUserCreationForm):
    """The admin's "Add user" form, with the fields this model needs.

    AdminUserCreationForm, not UserCreationForm: only the admin's version
    carries the `usable_password` choice, and naming that field on the plain
    form raises FieldError before the page loads.

    Django's add form asks only for username and password, so an account
    created here was saved with an empty email and could never sign in.
    """

    class Meta(AdminUserCreationForm.Meta):
        model = User
        fields = ("email", "username", "first_name", "last_name")

    # The same rules the API applies: the username becomes a public
    # /<username> page, so a reserved word would shadow a real route.
    def clean_username(self):
        return _as_form_error(validate_username, self.cleaned_data["username"])

    def clean_first_name(self):
        return _as_form_error(validate_name, self.cleaned_data["first_name"])

    def clean_last_name(self):
        return _as_form_error(validate_name, self.cleaned_data["last_name"])


class ProfileInline(admin.StackedInline):
    model = Profile
    extra = 0
    can_delete = False


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    # Appending, not replacing: replacing drops the permissions and dates
    # sections from the page.
    fieldsets = BaseUserAdmin.fieldsets + (
        ("Extra fields", {"fields": ("pending_email", "is_banned")}),
    )
    actions = ["ban_users", "unban_users"]
    add_form = UserCreateForm
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "username", "first_name", "last_name",
                       "usable_password", "password1", "password2"),
        }),
    )
    list_display = ("id", "email", "username", "is_staff", "is_active",
                    "is_banned", "date_joined")
    list_filter = ("is_staff", "is_active", "is_banned", "date_joined")
    search_fields = ("email", "username", "first_name", "last_name")
    ordering = ("-date_joined",)
    readonly_fields = ("date_joined", "last_login", "created_at", "updated_at")
    inlines = [ProfileInline]


    @admin.action(description="Ban selected users")
    def ban_users(self, request, queryset):
        """Both flags, in one action.

        `is_active` blocks the sign-in; `is_banned` stops an automatic path
        such as Google undoing it. Setting one alone is the mistake this
        action exists to prevent.
        """
        updated = queryset.update(is_banned=True, is_active=False)
        self.message_user(request, f"{updated} account(s) banned.")

    @admin.action(description="Lift the ban on selected users")
    def unban_users(self, request, queryset):
        """Lifts the ban, and restores only what the ban took away.

        An account that had signed in before was active when banned, so it
        goes back to active. One that never signed in was waiting for its
        activation mail; turning it on here would grant a confirmed status
        nobody proved, so the ban lifts and it stays inactive.
        """
        lifted = queryset.update(is_banned=False)
        restored = queryset.filter(last_login__isnull=False).update(is_active=True)
        self.message_user(
            request,
            f"{lifted} ban(s) lifted; {restored} account(s) reactivated. "
            f"Accounts that had never signed in stay inactive until they "
            f"confirm their email.",
        )


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "location", "is_identity_verified", "created_at")
    list_filter = ("is_identity_verified",)
    search_fields = ("user__email", "user__username")
    # A plain ForeignKey widget loads every user into a <select>: fine at 50
    # accounts, a page that never renders at 50,000.
    raw_id_fields = ("user",)
