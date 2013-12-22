"""Twitter source class.

Uses the v1.1 REST API: https://dev.twitter.com/docs/api

TODO: collections for twitter accounts; use as activity target?
TODO: reshare activities for retweets
"""

__author__ = ['Ryan Barrett <activitystreams@ryanb.org>']

import collections
import datetime
import json
import logging
import re
import urllib2
import urlparse

import appengine_config
import source
from oauth_dropins.twitter import TwitterAuth
from webutil import util

API_TIMELINE_URL = \
  'https://api.twitter.com/1.1/statuses/home_timeline.json?include_entities=true&count=%d'
API_SELF_TIMELINE_URL = \
  'https://api.twitter.com/1.1/statuses/user_timeline.json?include_entities=true&count=%d'
API_STATUS_URL = \
  'https://api.twitter.com/1.1/statuses/show.json?id=%s&include_entities=true'
API_RETWEETS_URL = \
  'https://api.twitter.com/1.1/statuses/retweets.json?id=%s'
API_USER_URL = \
  'https://api.twitter.com/1.1/users/lookup.json?screen_name=%s'
API_CURRENT_USER_URL = \
  'https://api.twitter.com/1.1/account/verify_credentials.json'
API_SEARCH_URL = \
    'https://api.twitter.com/1.1/search/tweets.json?q=%s&include_entities=true&result_type=recent&count=100'


class Twitter(source.Source):
  """Implements the ActivityStreams API for Twitter.
  """

  DOMAIN = 'twitter.com'
  NAME = 'Twitter'
  FRONT_PAGE_TEMPLATE = 'templates/twitter_index.html'

  def __init__(self, access_token_key, access_token_secret):
    """Constructor.

    Twitter now requires authentication in v1.1 of their API. You can get an
    OAuth access token by creating an app here: https://dev.twitter.com/apps/new

    Args:
      access_token_key: string, OAuth access token key
      access_token_secret: string, OAuth access token secret
    """
    self.access_token_key = access_token_key
    self.access_token_secret = access_token_secret

  def get_actor(self, screen_name=None):
    """Returns a user as a JSON ActivityStreams actor dict.

    Args:
      screen_name: string username. Defaults to the current user.
    """
    if screen_name is None:
      url = API_CURRENT_USER_URL
    else:
      url = API_USER_URL % screen_name
    return self.user_to_actor(json.loads(self.urlread(url)))

  def get_activities(self, user_id=None, group_id=None, app_id=None,
                     activity_id=None, start_index=0, count=0,
                     fetch_likes=False, fetch_shares=False):
    """Returns a (Python) list of ActivityStreams activities to be JSON-encoded.

    See method docstring in source.py for details.
    """
    if activity_id:
      tweets = [json.loads(self.urlread(API_STATUS_URL % activity_id))]
      total_count = len(tweets)
    else:
      url = API_SELF_TIMELINE_URL if group_id == source.SELF else API_TIMELINE_URL
      twitter_count = count + start_index
      tweets = json.loads(self.urlread(url % twitter_count))
      tweets = tweets[start_index:]
      total_count = None

    if fetch_shares:
      for tweet in tweets:
        if (not tweet.get('retweeted') and        # this *is not* a retweet
            tweet.get('retweet_count', 0) >= 1):  # this *has* retweets
          # store retweets in the 'retweets' field.
          # TODO: make these HTTP requests asynchronous. not easy since we don't
          # (yet) require threading support or use a non-blocking HTTP library.
          #
          # TODO: cache results or otherwise handle rate limiting. twitter
          # limits this API endpoint to one call per minute per user, which is
          # easy to hit.
          # https://dev.twitter.com/docs/rate-limiting/1.1/limits
          #
          # can't use the statuses/retweets_of_me endpoint because it only
          # returns the original tweets, not the retweets or their authors.
          url = API_RETWEETS_URL % tweet['id_str']
          tweet['retweets'] = json.loads(self.urlread(url))

    return total_count, [self.tweet_to_activity(t) for t in tweets]

  def get_comment(self, comment_id, activity_id=None):
    """Returns an ActivityStreams comment object.

    Args:
      comment_id: string comment id
      activity_id: string activity id, optional
    """
    url = API_STATUS_URL % comment_id
    return self.tweet_to_object(json.loads(self.urlread(url)))

  def get_like(self, activity_user_id, activity_id, like_user_id):
    """Returns an ActivityStreams 'like' activity object.

    Twitter's REST API doesn't have a way to fetch a tweet's individual
    favorites, just the total count. :/ The Streaming API can do it though.
    sigh.

    Args:
      activity_user_id: string id of the user who posted the original activity
      activity_id: string activity id
      like_user_id: string id of the user who liked the activity
    """
    return None

  def get_share(self, activity_user_id, activity_id, share_id):
    """Returns an ActivityStreams 'share' activity object.

    Args:
      activity_user_id: string id of the user who posted the original activity
      activity_id: string activity id
      share_id: string id of the share object
    """
    url = API_STATUS_URL % share_id
    return self.retweet_to_object(json.loads(self.urlread(url)))

  def urlread(self, url):
    """Wraps urllib2.urlopen() and adds an OAuth signature.
    """
    return TwitterAuth.signed_urlopen(
      url, self.access_token_key, self.access_token_secret, timeout=999).read()

  def tweet_to_activity(self, tweet):
    """Converts a tweet to an activity.

    Args:
      tweet: dict, a decoded JSON tweet

    Returns:
      an ActivityStreams activity dict, ready to be JSON-encoded
    """
    object = self.tweet_to_object(tweet)
    activity = {
      'verb': 'post',
      'published': object.get('published'),
      'id': object.get('id'),
      'url': object.get('url'),
      'actor': object.get('author'),
      'object': object,
      }

    reply_to_screenname = tweet.get('in_reply_to_screen_name')
    reply_to_id = tweet.get('in_reply_to_status_id')
    if reply_to_id and reply_to_screenname:
      activity['context'] = {
        'inReplyTo': [{
          'objectType': 'note',
          'id': self.tag_uri(reply_to_id),
          'url': self.status_url(reply_to_screenname, reply_to_id),
          }]
        }

    # yes, the source field has an embedded HTML link. bleh.
    # https://dev.twitter.com/docs/api/1.1/get/statuses/show/
    parsed = re.search('<a href="([^"]+)".*>(.+)</a>', tweet.get('source', ''))
    if parsed:
      url, name = parsed.groups()
      activity['generator'] = {'displayName': name, 'url': url}

    return self.postprocess_activity(activity)

  def tweet_to_object(self, tweet):
    """Converts a tweet to an object.

    Args:
      tweet: dict, a decoded JSON tweet

    Returns:
      an ActivityStreams object dict, ready to be JSON-encoded
    """
    object = {}

    # always prefer id_str over id to avoid any chance of integer overflow.
    # usually shouldn't matter in Python, but still.
    id = tweet.get('id_str')
    if not id:
      return {}

    object = {
      'objectType': 'note',
      'published': self.rfc2822_to_iso8601(tweet.get('created_at')),
      # don't linkify embedded URLs. (they'll all be t.co URLs.) instead, use
      # url entities below to replace them with the real URLs, and then linkify.
      'content': tweet.get('text'),
      'attachments': [],
      }

    user = tweet.get('user')
    if user:
      object['author'] = self.user_to_actor(user)
      username = object['author'].get('username')
      if username:
        object['id'] = self.tag_uri(id)
        object['url'] = self.status_url(username, id)

    entities = tweet.get('entities', {})

    # currently the media list will only have photos. if that changes, though,
    # we'll need to make this conditional on media.type.
    # https://dev.twitter.com/docs/tweet-entities
    media_url = entities.get('media', [{}])[0].get('media_url')
    if media_url:
      object['image'] = {'url': media_url}
      object['attachments'].append({
          'objectType': 'image',
          'image': {'url': media_url},
          })

    # tags
    object['tags'] = [
      {'objectType': 'person',
       'id': self.tag_uri(t.get('screen_name')),
       'url': self.user_url(t.get('screen_name')),
       'displayName': t.get('name'),
       'indices': t.get('indices')
       } for t in entities.get('user_mentions', [])
      ] + [
      {'objectType': 'hashtag',
       'url': 'https://twitter.com/search?q=%23' + t.get('text'),
       'indices': t.get('indices'),
       } for t in entities.get('hashtags', [])
      ] + [
      # TODO: links are both tags and attachments right now. should they be one
      # or the other?
      # file:///home/ryanb/docs/activitystreams_schema_spec_1.0.html#tags-property
      # file:///home/ryanb/docs/activitystreams_json_spec_1.0.html#object
      {'objectType': 'article',
       'url': t.get('expanded_url'),
       # TODO: elide full URL?
       'indices': t.get('indices'),
       } for t in entities.get('urls', [])
      ]
    for t in object['tags']:
      indices = t.get('indices')
      if indices:
        t.update({
            'startIndex': indices[0],
            'length': indices[1] - indices[0],
            })
        del t['indices']

    # retweets
    object['tags'] += [self.retweet_to_object(r) for r in tweet.get('retweets', [])]

    # location
    place = tweet.get('place')
    if place:
      object['location'] = {
        'displayName': place.get('full_name'),
        'id': place.get('id'),
        }

      # place['url'] is a JSON API url, not useful for end users. get the
      # lat/lon from geo instead.
      geo = tweet.get('geo')
      if geo:
        coords = geo.get('coordinates')
        if coords:
          object['location']['url'] = ('https://maps.google.com/maps?q=%s,%s' %
                                       tuple(coords))

    return util.trim_nulls(object)

  def user_to_actor(self, user):
    """Converts a tweet to an activity.

    Args:
      user: dict, a decoded JSON Twitter user

    Returns:
      an ActivityStreams actor dict, ready to be JSON-encoded
    """
    username = user.get('screen_name')
    if not username:
      return {}

    return util.trim_nulls({
      'displayName': user.get('name'),
      'image': {'url': user.get('profile_image_url')},
      'id': self.tag_uri(username) if username else None,
      'published': self.rfc2822_to_iso8601(user.get('created_at')),
      'url': self.user_url(username),
      'location': {'displayName': user.get('location')},
      'username': username,
      'description': user.get('description'),
      })

  def retweet_to_object(self, retweet):
    """Converts a retweet to a share activity object.

    Args:
      retweet: dict, a decoded JSON tweet

    Returns:
      an ActivityStreams object dict
    """
    orig = retweet.get('retweeted_status')
    if not orig:
      return None

    share = self.tweet_to_object(retweet)
    share.update({
        'objectType': 'activity',
        'verb': 'share',
        'object': {'url': self.status_url(orig.get('user', {}).get('screen_name'),
                                          orig.get('id_str'))},
        'content': 'retweeted this.',
        })
    return share

  @staticmethod
  def rfc2822_to_iso8601(time_str):
    """Converts a timestamp string from RFC 2822 format to ISO 8601.

    Example RFC 2822 timestamp string generated by Twitter:
      'Wed May 23 06:01:13 +0000 2007'

    Resulting ISO 8610 timestamp string:
      '2007-05-23T06:01:13'
    """
    if not time_str:
      return None

    without_timezone = re.sub(' [+-][0-9]{4} ', ' ', time_str)
    dt = datetime.datetime.strptime(without_timezone, '%a %b %d %H:%M:%S %Y')
    return dt.isoformat()

  @classmethod
  def user_url(cls, username):
    """Returns the Twitter URL for a given user."""
    return 'http://%s/%s' % (cls.DOMAIN, username)

  @classmethod
  def status_url(cls, username, id):
    """Returns the Twitter URL for a tweet from a given user with a given id."""
    return '%s/status/%s' % (cls.user_url(username), id)
