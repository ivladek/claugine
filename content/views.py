"""The public pages: the home page, an article, the guide redirect, and the staff pages that edit them.

The guide addresses redirect to `DOCS_URL`: the portal has had no documentation of its own since 2026-10-26.
"""
from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l

from accounts.models import Role, audit
from accounts.utils import staff_required

from .models import Article, ServiceComponent


def home_view(request):
    components = list(ServiceComponent.objects.all())
    worst = "operational"
    order = ["operational", "maintenance", "degraded", "outage"]
    for c in components:
        if order.index(c.state) > order.index(worst):
            worst = c.state
    return render(request, "content/home.html", {
        "components": [(c, c.uptime()) for c in components],
        "overall": worst,
        "announcements": Article.visible(Article.Kind.ANNOUNCEMENT)[:5],
        "news": Article.visible(Article.Kind.NEWS)[:10],
    })


def article_view(request, pk):
    article = get_object_or_404(Article, pk=pk, is_published=True)
    return render(request, "content/article.html", {"article": article})


class ArticleForm(forms.ModelForm):
    class Meta:
        model = Article
        fields = ["kind", "title", "body", "is_published", "show_until"]
        widgets = {
            "body": forms.Textarea(attrs={"rows": 10}),
            "show_until": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        }


class ComponentForm(forms.ModelForm):
    class Meta:
        model = ServiceComponent
        fields = ["name", "state", "note", "position"]


@staff_required(Role.SUPPORT)
def manage_articles_view(request):
    articles = list(Article.objects.select_related("author")[:200])
    components = list(ServiceComponent.objects.all())
    return render(request, "content/manage_list.html", {
        "articles": [(a, ArticleForm(instance=a, auto_id=f"a{a.pk}_%s")) for a in articles],
        "components": [(c, ComponentForm(instance=c, auto_id=f"c{c.pk}_%s")) for c in components],
        "article_form": ArticleForm(auto_id="anew_%s"), "component_form": ComponentForm(auto_id="cnew_%s"),
    })


@staff_required(Role.SUPPORT)
def edit_article_view(request, pk=None):
    article = get_object_or_404(Article, pk=pk) if pk else None
    form = ArticleForm(request.POST or None, instance=article)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        if obj.author_id is None:
            obj.author = request.user
        obj.save()
        audit(request.user, "article_saved", obj, title=obj.title, published=obj.is_published)
        messages.success(request, _("Saved."))
        return redirect("manage_articles")
    return render(request, "content/edit.html", {"form": form, "object": article, "what": _("article")})


@staff_required(Role.SUPPORT)
def edit_component_view(request, pk=None):
    comp = get_object_or_404(ServiceComponent, pk=pk) if pk else None
    form = ComponentForm(request.POST or None, instance=comp)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        audit(request.user, "component_saved", obj, name=obj.name, state=obj.state)
        messages.success(request, _("Saved."))
        return redirect("manage_articles")
    return render(request, "content/edit.html", {"form": form, "object": comp, "what": _("service")})


@staff_required(Role.SUPPORT)
def delete_content_view(request, what, pk):
    from django.views.decorators.http import require_POST  # noqa: F401 - POST only below
    if request.method != "POST":
        return redirect("manage_articles")
    model = {"article": Article, "service": ServiceComponent}.get(what)
    if model is None:
        return redirect("manage_articles")
    obj = get_object_or_404(model, pk=pk)
    label = getattr(obj, "title", None) or getattr(obj, "name", "")
    obj.delete()
    audit(request.user, f"{what}_deleted", None, name=label)
    messages.success(request, _("Deleted."))
    return redirect("manage_articles")


# --- documentation: on the public docs site ------------------------------------------------------------------------

GUIDE_PAGES = [  # (slug, title)
    ("service", _l("The service")),
    ("architecture", _l("Platform and architecture")),
    ("services", _l("Services for projects")),
    ("org-admin", _l("Organization admin guide")),
    ("tenant-user", _l("Project user guide")),
    ("staff", _l("Platform users guide")),
]
REFERENCE_PAGES = {"portal": "reference/portal/", "data": "reference/data-model/", "network": "reference/network/"}
DOCS_LANG = {"ru": "ru/", "kk": "kz/"}  # English is the site's root


def docs_url(path=""):
    """A page of the public documentation (settings.DOCS_URL) in the user's language where the site has it."""
    from django.conf import settings
    from django.utils.translation import get_language
    lang = DOCS_LANG.get((get_language() or "en").split("-")[0], "")
    if path.startswith("reference/"):
        lang = ""  # the reference is in English only
    return f"{settings.DOCS_URL}/{lang}{path}"


def docs_links():
    """For the templates: the root and every guide of the public documentation."""
    links = {slug.replace("-", "_"): docs_url(f"guide/{slug}/") for slug, _t in GUIDE_PAGES}
    links["root"] = docs_url()
    links["reference"] = docs_url(REFERENCE_PAGES["portal"])
    return links


def guide_view(request, page=None):
    """The guides moved to the public documentation: old links lead there."""
    from django.http import Http404
    if page is None:
        return redirect(docs_url())
    if page not in dict(GUIDE_PAGES):
        raise Http404
    return redirect(docs_url(f"guide/{page}/"))
