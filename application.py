from flask import Flask, session, render_template, redirect, request, url_for
from flask_session import Session
import httplib2
from apiclient import discovery
from oauth2client import client
import json
import os
import urllib
from urllib.parse import urlparse
import operator

from celery import Celery

import uuid


PLAYLISTGENIUS_DEV_KEY = os.environ.get('PLAYLISTGENIUS_DEV_KEY')
if PLAYLISTGENIUS_DEV_KEY is None:
    exit('Developer key needed. You can export as PLAYLIST_DEV_KEY')

app = Flask(__name__)
app.secret_key = str(uuid.uuid4())


youtube = discovery.build('youtube', 'v3', developerKey=PLAYLISTGENIUS_DEV_KEY)

#Celery for MQ
app.config['broker_url'] = 'amqp://guest:guest@localhost//'
app.config['result_backend'] = 'amqp://localhost//'
celery = Celery(app.name, broker=app.config['broker_url'])
celery.conf.update(app.config)
celery.conf.task_default_queue = 'test'



TaskBase = celery.Task
class ContextTask(TaskBase):
    abstract = True
    def __call__(self, *args, **kwargs):
        with app.app_context():
            return TaskBase.__call__(self, *args, **kwargs)
        celery.Task = ContextTask


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/playlist/<pUrlId>')
def sendPlaylist(pUrlId, maxLength=50):
    #playlist id
    #pUrl = urlparse(request.args.get('playlist_url'))
    #TODO error handling when not playlist or wrong url
    #pUrlId = ''.join(urllib.parse.parse_qs(pUrl.query)['list'])

    #get a the list of videos in playlist and extract the ids
    playlist = youtube.playlistItems().list(
        part='contentDetails',
        maxResults=50,
        playlistId=pUrlId
        ).execute()
    playlistIds = extractIds(playlist, pUrlId)

    #get ids of related videos
    related = getRelated(playlistIds)

    #cut list of ids in order to have the size wanted + order it
    if len(related) < maxLength:
        maxLength = len(related)
    relatedSorted = sorted(
        related.items(),
        key=lambda x: (x[1]['count'],
                       x[1]['relevance']
                      ), reverse=True)[:maxLength]

    return render_template('playlist.html', playlist=relatedSorted)


def extractIds(playlist, pId=None):
    try:
        playlistIds
    except NameError:
        playlistIds = []
    for item in playlist['items']:
        playlistIds.append(item['contentDetails']['videoId'])
    #new request if playlist > 50 items
    if 'nextPageToken' in playlist:
        youtube = discovery.build('youtube', 'v3', developerKey=PLAYLISTGENIUS_DEV_KEY)
        playlistNext = youtube.playlistItems().list(
            part='contentDetails',
            maxResults=50,
            playlistId=pId,
            pageToken=playlist['nextPageToken']
            ).execute()
        playlistIds.extend(extractIds(playlistNext, pId))
        return playlistIds
    else:
        return playlistIds

@celery.task
def extractVideosHttp(pId, forNext=None):
    if forNext == None:
        playlist = youtube.playlistItems().list(
            part='contentDetails',
            maxResults=50,
            playlistId=pId
            ).execute()
    else:
        playlist = youtube.playlistItems().list(
            part='contentDetails',
            maxResults=50,
            playlistId=pId,
            pageToken=playlist['nextPageToken']
            ).execute()
    return playlist


def getRelated(videoIds, maxResults=30, underground=False):
    #underground = True
    if underground == True:
        maxResults = 50
    relatedVideos = {}
    videoIndex = 1
    playlistTotal = len(videoIds)
    videoGlobalTotal = playlistTotal * maxResults
    for vId in videoIds:
        relevanceIndex = 1
        relevant_ids = []
        videoTotal = maxResults
        related_async = getRelatedHttp.delay(vId, maxResults)
        related = related_async.wait()
        relevant_ids = [video['id']['videoId'] for video in related['items']]

        if underground == True:
            undergrounds = onlyUnderground(related, vId=vId)
            relevant_ids = [underg[0] for underg in undergrounds]
            related = youtube.search().list(
                part='snippet',
                maxResults=maxResults,
                order='relevance',
                relatedToVideoId=videoId,
                type='video',
                pageToken=nextPageToken
                ).execute()

        for video in related['items']:
            if video['id']['videoId'] in relatedVideos:
                currentCount = relatedVideos[video['id']['videoId']]['count']
                currentRelevance = relatedVideos[video['id']['videoId']]['relevance']
                relatedVideos[video['id']['videoId']]['count'] = currentCount + 1
                relatedVideos[video['id']['videoId']]['relevance'] = (
                    (currentCount*currentRelevance)+relevanceIndex
                    )/(currentCount+1)

            elif video['id']['videoId'] not in videoIds:
                count = 1
                relatedVideos[video['id']['videoId']] = {
                    'count': count,
                    'relevance': relevanceIndex,
                    'title': video['snippet']['title'],
                    'thumbnail': video['snippet']['thumbnails']['default']['url']

                }
                relevanceIndex -= 1/len(related['items'])
        videoIndex += 1
    return relatedVideos

def onlyUnderground(videos, vId,
                    whatsUnderground=100000, nextPageToken=None,
                    sumVideo=0, deep=-1):

    list_ids = []
    if nextPageToken is not None:
        videos_asyncs = getRelatedHttp.delay(vId, maxResults=50, nextPageToken=nextPageToken)
        videos = videos_asyncs.wait()
    if deep >= 0:
        videos_asyncs = getRelatedHttp.delay(vId, maxResults=50)
        videos = videos_asyncs.wait()
    for video in videos['items']:
        list_ids.append(video['id']['videoId'])
    if len(list_ids) > 50:
        list_ids = list_ids[:50]
    list_ids = ','.join(list_ids)
    statVideos = youtube.videos().list(
        part='statistics',
        id=list_ids
    ).execute()
    undergroundVideos = []
    for statVideo in statVideos['items']:
        try:
            if int(statVideo['statistics']['viewCount']) < whatsUnderground:
                undergroundVideos.append(
                    (statVideo['id'],
                     int(statVideo['statistics']['viewCount']))
                    )
        except KeyError:
            pass
    sumVideo += len(undergroundVideos)

    while True:
        if sumVideo > 30:
            break
        if 'nextPageToken' not in videos:
            #take the first element in the list and get relevant items -> deepen the search
            deep += 1
            undergroundVideos.extend(
                onlyUnderground(
                    videos=None,
                    vId=undergroundVideos[deep][0],
                    nextPageToken=None,
                    sumVideo=sumVideo,
                    deep=deep)
            )
            sumVideo = len(undergroundVideos)
            continue
        undergroundVideos.extend(onlyUnderground(
            videos=None,
            vId=vId,
            nextPageToken=videos['nextPageToken'],
            sumVideo=sumVideo)
                                )
        sumVideo = len(undergroundVideos)
    return undergroundVideos


@celery.task
def getRelatedHttp(videoId, maxResults=30, nextPageToken=None):
    related = youtube.search().list(
        part='snippet',
        maxResults=maxResults,
        order='relevance',
        relatedToVideoId=videoId,
        type='video',
        pageToken=nextPageToken
        ).execute()
    return related



@app.route('/createPlaylist', methods=['GET', 'POST', 'PUT'])
def createPlaylist():
    return render_template('mynewplaylist.html', newPlaylist='https://youtube.com/playlist?list=PLv2iKoUrni8OL93-7pvC_ZWiQfDVmCEnr')
    session['playlist'] = json.dumps(dict(request.form))
    if 'credentials' not in session:
        session['prev_page'] = 'createPlaylist'
        return redirect(url_for('authentificated'))
    credentials = client.OAuth2Credentials.from_json(session['credentials'])
    if credentials.access_token_expired:
        session['prev_page'] = 'createPlaylist'
        return redirect(url_for('authentificated'))
    playlist = json.loads(session.get('playlist'))

    http_auth = credentials.authorize(httplib2.Http())
    youtube = discovery.build('youtube', 'v3', http=http_auth)
    newPlaylist = youtube.playlists().insert(
        part="snippet",
        body=dict(
            snippet=dict(
                title=playlist['playlist_title']
            )
        )
    ).execute()
    del(playlist['playlist_title'])
    for key, value in playlist.items():
        fillPlaylist = youtube.playlistItems().insert(
            part='snippet',
            body={
                'snippet': {
                    'playlistId':newPlaylist['id'],
                    'resourceId': {
                        'kind': 'youtube#video',
                        'videoId': key
                    }
                }
            }
        ).execute()
    return render_template('mynewplaylist.html', newPlaylist='https://youtube.com/playlist?list='+newPlaylist['id'])

@app.route('/authentificated')
def authentificated():
    flow = client.flow_from_clientsecrets(
        '/home/witoldw/dev/youtube-genius/secret.json',
        scope=' https://www.googleapis.com/auth/youtube',
        redirect_uri=url_for('authentificated', _external=True)
    )
    if 'code' not in request.args:
        auth_uri = flow.step1_get_authorize_url()
        return redirect(auth_uri)
    else:
        auth_code = request.args.get('code')
        credentials = flow.step2_exchange(auth_code)
        session['credentials'] = credentials.to_json()
        return redirect(url_for(session['prev_page']))

@app.route('/user/playlists')
def user():
    if 'credentials' not in session:
        session['prev_page'] = 'user'
        return redirect(url_for('authentificated', prev_page='user'))
    credentials = client.OAuth2Credentials.from_json(session['credentials'])
    if credentials.access_token_expired:
        session['prev_page'] = 'user'
        return redirect(url_for('authentificated'))
    else:
        http_auth = credentials.authorize(httplib2.Http())
        youtube = discovery.build('youtube', 'v3', http=http_auth)
        allPlaylists = youtube.playlists().list(
            part="snippet,id",
            mine=True,
            maxResults=50
        ).execute()
        playlists = []
        for p in allPlaylists['items']:
            snippet = p['snippet']
            playlist = {}
            playlist['thumbnail'] = snippet['thumbnails']['high']['url']
            playlist['description'] = snippet['localized']['description']
            playlist['title'] = snippet['localized']['title']
            playlist['id'] = p['id']
            playlists.append(playlist)
        return render_template('playlists.html', playlists=playlists)



if __name__ == "__main__":
    SESSION_TYPE = 'redis'
    Session(app)

    app.run(host='0.0.0.0')

