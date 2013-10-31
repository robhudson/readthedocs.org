import hashlib
import logging

from django.shortcuts import get_object_or_404
from django.template import Template, Context
from django.conf import settings

from distlib.version import UnsupportedVersionError
from rest_framework import decorators, permissions, viewsets, status
from rest_framework.renderers import JSONPRenderer, JSONRenderer, BrowsableAPIRenderer
from rest_framework.response import Response
import requests

from betterversion.better import version_windows, BetterVersion 
from builds.models import Version
from djangome import views as djangome
from search.indexes import PageIndex, ProjectIndex
from projects.models import Project, EmailHook

from .serializers import ProjectSerializer
from .permissions import RelatedProjectIsOwner
import utils as api_utils


log = logging.getLogger(__name__)

class ProjectViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    renderer_classes = (JSONRenderer, JSONPRenderer, BrowsableAPIRenderer)
    model = Project

    @decorators.link()
    def valid_versions(self, request, **kwargs):
        """
        Maintain state of versions that are wanted.
        """
        project = get_object_or_404(Project, pk=kwargs['pk'])
        if not project.num_major or not project.num_minor or not project.num_point:
            return Response({'error': 'Project does not support point version control'}, status=status.HTTP_400_BAD_REQUEST)
        versions = []
        for ver in project.versions.all():
            try:
                versions.append(BetterVersion(ver.verbose_name))
            except UnsupportedVersionError:
                # Probably a branch
                pass
        active_versions = version_windows(
            versions, 
            major=project.num_major, 
            minor=project.num_minor, 
            point=project.num_point,
            flat=True,
        )
        version_strings = [v._string for v in active_versions]
        # Disable making old versions inactive for now.
        #project.versions.exclude(verbose_name__in=version_strings).update(active=False)
        project.versions.filter(verbose_name__in=version_strings).update(active=True)
        return Response({
            'flat': version_strings,
            })

    @decorators.link()
    def translations(self, request, **kwargs):
        project = get_object_or_404(Project, pk=kwargs['pk'])
        queryset = project.translations.all()
        return Response({
            'translations': ProjectSerializer(queryset, many=True).data
        })


    @decorators.action(permission_classes=[permissions.IsAdminUser])
    def sync_versions(self, request, **kwargs):
        """
        Sync the version data in the repo (on the build server) with what we have in the database.

        Returns the identifiers for the versions that have been deleted.
        """
        project = get_object_or_404(Project, pk=kwargs['pk'])
        try:
            data = request.DATA
            added_versions = set()
            if 'tags' in data:
                ret_set = api_utils.sync_versions(project, data['tags'])
                added_versions.update(ret_set)
            if 'branches' in data:
                ret_set = api_utils.sync_versions(project, data['branches'])
                added_versions.update(ret_set)
            deleted_versions = api_utils.delete_versions(project, data)
            return Response({
                'added_versions': added_versions,
                'deleted_versions': deleted_versions,
            })       
        except Exception, e:
            log.exception(e.message)
            return Response({'error': e.message}, status=status.HTTP_400_BAD_REQUEST)


class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = (permissions.IsAuthenticated, RelatedProjectIsOwner)
    renderer_classes = (JSONRenderer, BrowsableAPIRenderer)
    model = EmailHook

    def get_queryset(self):
        """
        This view should return a list of all the purchases
        for the currently authenticated user.
        """
        user = self.request.user
        if user.is_superuser:
            return self.model.objects.all()
        return self.model.objects.filter(project__users__in=[user.pk])

class VersionViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    renderer_classes = (JSONRenderer, BrowsableAPIRenderer)
    model = Version

    @decorators.link()
    def downloads(self, request, **kwargs):
        version = get_object_or_404(Version, pk=kwargs['pk'])
        downloads = version.get_downloads(pretty=True)
        return Response({
            'downloads': downloads
        })

TEMPLATE = """
<div class="injected">

  {% if not new_theme %}
  <div class="rst-versions rst-badge" data-toggle="rst-versions">
    <span class="rst-current-version" data-toggle="rst-current-version">
      <span class="icon icon-book">&nbsp;</span>
      v: {{ current_version }}
      <span class="icon icon-caret-down"></span>
    </span>
    <div class="rst-other-versions">
  {% endif %}
  
      <dl>
        <dt>Versions</dt>
        {% for version in versions %}
          {% if version.slug == current_version %}
          <strong>
          {% endif %}
          <dd><a href="{{ version.get_subdomain_url }}">{{ version.slug }}</a></dd>
          {% if version.slug == current_version %}
          </strong>
          {% endif %}
        {% endfor %}
      </dl>
      <dl>
        <dt>Downloads</dt>
        {% for name, url in downloads.items %}
          <dd><a href="{{ url }}">{{ name }}</a></dd>
        {% endfor %}
      </dl>
      <dl>
        <dt>On Read the Docs</dt>
          <dd>
            <a href="//{{ settings.PRODUCTION_DOMAIN }}/projects/{{ project.slug }}/?fromdocs={{ project.slug }}">Project Home</a>
          </dd>
          <dd>
            <a href="//{{ settings.PRODUCTION_DOMAIN }}/builds/{{ project.slug }}/?fromdocs={{ project.slug }}">Builds</a>
          </dd>
      </dl>
      {% if github_url %}
      <dl>
        <dt>On GitHub</dt>
          <dd>
            <a href="{{ github_url }}">Edit</a>
          </dd>
      </dl>
      {% elif bitbucket_url %}
      <dl>
        <dt>On Bitbucket</dt>
          <dd>
            <a href="{{ bitbucket_url }}">Edit</a>
          </dd>
      </dl>
      {% endif %}
      <hr/>
      Free document hosting provided by <a href="http://www.readthedocs.org">Read the Docs</a>.

  {% if not new_theme %}
    </div>
  </div>
  {% endif %}

</div>
"""

@decorators.api_view(['GET'])
@decorators.permission_classes((permissions.AllowAny,))
@decorators.renderer_classes((JSONRenderer, JSONPRenderer, BrowsableAPIRenderer))
def footer_html(request):
    project_slug = request.GET.get('project', None)
    version_slug = request.GET.get('version', None)
    page_slug = request.GET.get('page', None)
    theme = request.GET.get('theme', False)
    docroot = request.GET.get('docroot', '')
    new_theme = (theme == "sphinx_rtd_theme")
    using_theme = (theme == "default")
    project = get_object_or_404(Project, slug=project_slug)
    version = project.versions.get(slug=version_slug)
    context = Context({
        'project': project,
        'downloads': version.get_downloads(pretty=True),
        'current_version': version.slug,
        'versions': project.ordered_active_versions(),
        'using_theme': using_theme,
        'new_theme': new_theme,
        'settings': settings,
        'github_url': version.get_github_url(docroot, page_slug),
        'bitbucket_url': version.get_bitbucket_url(docroot, page_slug),
    })
    html = Template(TEMPLATE).render(context)
    return Response({"html": html})

@decorators.api_view(['GET'])
@decorators.permission_classes((permissions.AllowAny,))
@decorators.renderer_classes((JSONRenderer, JSONPRenderer, BrowsableAPIRenderer))
def quick_search(request):
    project_slug = request.GET.get('project', None)
    version_slug = request.GET.get('version', 'latest')
    query = request.GET.get('q', None)
    redis_data = djangome.r.keys('redirects:v4:en:%s:%s:*%s*' % (version_slug, project_slug, query))
    ret_dict = {}
    for data in redis_data:
        if 'http://' in data or 'https://' in data:
            key = data.split(':')[5]
            value = ':'.join(data.split(':')[6:])
            ret_dict[key] = value
    return Response({"results": ret_dict})

@decorators.api_view(['POST'])
@decorators.permission_classes((permissions.IsAdminUser,))
@decorators.renderer_classes((JSONRenderer, JSONPRenderer, BrowsableAPIRenderer))
def index_search(request):
    page_obj = PageIndex()
    data = request.DATA['data']
    page_list = data['page_list']
    project_pk = data['project_pk']
    version_pk = data['version_pk']
    project = Project.objects.get(pk=project_pk)
    version = Version.objects.get(pk=version_pk)
    resp = requests.get('https://api.grokthedocs.com/api/v1/index/1/heatmap/', params={'project': project.slug, 'compare': True})
    ret_json = resp.json()
    project_scale = ret_json['scaled_project'][project.slug]

    project_obj = ProjectIndex()
    project_obj.index_document({
            'id': project.pk,
            'name': project.name,
            'slug': project.slug,
            'description': project.description,
            'lang': project.language,
            'author': [user.username for user in project.users.all()],
            'url': project.get_absolute_url(),
            '_boost': project_scale,
        })

    index_list = []
    for page in page_list:
        page_scale = ret_json['scaled_page'].get(page['path'], 1)
        page['_boost'] = page_scale + project_scale
        page['project'] = project.slug
        page['version'] = version.slug
        page['id'] = hashlib.md5('%s-%s-%s' % (project.slug, version.slug, page['path']) ).hexdigest()
        index_list.append(page)
    page_obj.bulk_index(index_list, parent=project_pk)
    return Response({'indexed': True})

@decorators.api_view(['GET'])
@decorators.permission_classes((permissions.AllowAny,))
@decorators.renderer_classes((JSONRenderer, JSONPRenderer, BrowsableAPIRenderer))
def search(request):
    project_slug = request.GET.get('project', None)
    version_slug = request.GET.get('version', 'latest')
    query = request.GET.get('q', None)

    if project_slug:
        project = Project.objects.get(slug=project_slug)
        # This is a search within a project -- do a Page search.
        results = (
            PageIndex().search()
            .filter(project=project.slug, version=version_slug)
            .query(title__match=query, headers__match=query,
                   content__match=query, should=True)
            .boost(title__match=10, headers__match=5)
            .facet('path')
            .highlight('title', 'headers', 'content')
            .values_dict('title', 'project', 'version', 'path')
            .execute())

    else:
        results = (
            ProjectIndex().search()
            .query(name__match=query, description__match=query, should=True)
            .boost(name__match=10)
            .values_dict('name', 'slug', 'description', 'lang')
            .execute())

    response = {}
    if hasattr(results, 'facets'):
        response['facets'] = results.facets
    response['hits'] = results.results

    return Response(response)
