import flask
from flask import Flask, session
from flask import render_template, redirect, request
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
if PLAYLISTGENIUS_DEV_KEY == None:
    exit('Developer key needed. You can export as PLAYLIST_DEV_KEY')

app = Flask(__name__)
app.secret_key = str(uuid.uuid4())

#Celery for MQ
app.config['broker_url'] = 'amqp://guest:guest@localhost//'
app.config['result_backend'] = 'amqp://localhost//'
celery = Celery(app.name, broker=app.config['broker_url'])
celery.conf.update(app.config)
celery.conf.task_default_queue ='test'


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

@app.route('/playlist')
def sendPlaylist(maxLength=50):
    #playlist id
    pUrl = urlparse(request.args.get('playlist_url'))
    pUrlId = ''.join(urllib.parse.parse_qs(pUrl.query)['list'])

    #get a the list of videos in playlist and extract the ids
    youtube = discovery.build('youtube', 'v3', developerKey=PLAYLISTGENIUS_DEV_KEY)
    playlist = youtube.playlistItems().list(part='contentDetails', maxResults=50, playlistId=pUrlId).execute()
    playlistIds = extractIds(playlist, pUrlId)

    #get ids of related videos
    related = getRelated(playlistIds)

    #cut list of ids in order to have the size wanted + order it
    if len(related) < maxLength:
        maxLength = len(related)
    relatedSorted = sorted(related.items(),key=lambda x: (x[1]['count'], x[1]['relevance']),reverse=True)[:maxLength]

    return render_template('playlist.html', playlist=relatedSorted) 


def extractIds(playlist, pId):
    try:
        playlistIds
    except NameError:
        playlistIds = []
    for item in playlist['items']:
        playlistIds.append(item['contentDetails']['videoId'])
        #new request if playlist > 50 items
        if 'nextPageToken' in playlist:
            youtube = discovery.build('youtube', 'v3', developerKey='AIzaSyBCcDOOjtPIukK_L77INPjg3MSKXajO16U')
            playlistNext = youtube.playlistItems().list(part='contentDetails', maxResults=50, playlistId=pId, pageToken=playlist['nextPageToken']).execute()
            playlistIds.extend(extractIds(playlistNext, pId))
            return playlistIds
        else:
            return playlistIds
@celery.task
def extractVideosHttp(pId, forNext=None):
    youtube = discovery.build('youtube', 'v3', developerKey='AIzaSyBCcDOOjtPIukK_L77INPjg3MSKXajO16U')
    if forNext == None:
        playlist = youtube.playlistItems().list(part='contentDetails', maxResults=50, playlistId=pId).execute()
    else: 
        playlist = youtube.playlistItems().list(part='contentDetails', maxResults=50, playlistId=pId, pageToken=playlist['nextPageToken']).execute()
    return playlist 


def getRelated(videoIds, maxResults=30, undergroud=False):
    relatedVideos = {}
    videoIndex= 1
    relevanceIndex = 1
    playlistTotal = len(videoIds)
    videoGlobalTotal = playlistTotal * maxResults
    for vId in videoIds:
#        youtube = discovery.build('youtube', 'v3', developerKey='AIzaSyBCcD    OOjtPIukK_L77INPjg3MSKXajO16U')
#        related = youtube.search().list(part='snippet', maxResults=maxResults,order='relevance', relatedToVideoId=vId, type='video').execute()
        videoTotal = maxResults
        related_asyncs = [getRelatedHttp.delay(vId, maxResults) for vId in videoIds] 
        relatedS = [related_async.wait() for related_async in related_asyncs]
        for related in relatedS:
            for video in related['items']:
                if video['id']['videoId'] in relatedVideos:
                    currentCount = relatedVideos[video['id']['videoId']]['count']
                    currentRelevance = relatedVideos[video['id']['videoId']]['relevance']
                    relatedVideos[video['id']['videoId']]['count'] = currentCount + 1
                    relatedVideos[video['id']['videoId']]['relevance'] = ((currentCount*currentRelevance)+relevanceIndex)/(currentCount+1)
                elif video['id']['videoId'] not in videoIds:
                    count = 1
                    relatedVideos[video['id']['videoId']] = {
                        'count': count,
                        'relevance': relevanceIndex,
                        'title': video['snippet']['title'],
                        'thumbnail': video['snippet']['thumbnails']['default']['url']

                    }
                    relevanceIndex -= 1/videoGlobalTotal
                    videoIndex +=1

        return relatedVideos

@celery.task
def getRelatedHttp(videoId, maxResults):
    #youtube = discovery.build('youtube', 'v3', developerKey='AIzaSyBCcDOOjtPIukK_L77INPjg3MSKXajO16U')
    youtube = discovery.build('youtube', 'v3', developerKey='AIzaSyBCcDOOjtPIukK_L77INPjg3MSKXajO16U')
    related = youtube.search().list(part='snippet', maxResults=maxResults,order='relevance', relatedToVideoId=videoId, type='video').execute()
    return related

@app.route('/createPlaylist', methods=['GET', 'POST', 'PUT'])
def createPlaylist():
    session['playlist'] = json.dumps(dict(request.form))
    if 'credentials' not in flask.session:
        flask.session['prev_page']='createPlaylist'
        return flask.redirect(flask.url_for('authentificated'))
    credentials = client.OAuth2Credentials.from_json(flask.session['credentials'])
    if credentials.access_token_expired:
        flask.session['prev_page']='createPlaylist'
        return flask.redirect(flask.url_for('authentificated'))
    playlist = json.loads(session.get('playlist'))

    http_auth = credentials.authorize(httplib2.Http())
    youtube = discovery.build('youtube', 'v3', http=http_auth)
    newPlaylist = youtube.playlists().insert(
        part = "snippet",
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
        return 'https://youtube.com/playlist?list='+newPlaylisrt['id'] 

@app.route('/authentificated')
def authentificated():
    flow = client.flow_from_clientsecrets(
        'secret.json',
        scope=' https://www.googleapis.com/auth/youtube',
        redirect_uri=flask.url_for('authentificated', _external=True)
    )
    if 'code' not in flask.request.args:
        auth_uri = flow.step1_get_authorize_url()
        return flask.redirect(auth_uri)
    else:
        auth_code = flask.request.args.get('code')
        credentials = flow.step2_exchange(auth_code)
        flask.session['credentials'] = credentials.to_json()
        return flask.redirect(flask.url_for(flask.session['prev_page']))

@app.route('/user/playlists')
def user():
    if 'credentials' not in flask.session:
        return flask.redirect(flask.url_for('authentificated', prev_page='user'))
    credentials = client.OAuth2Credentials.from_json(flask.session['credentials'])
    if credentials.access_token_expired:
        flask.session['prev_page'] = 'user'
        return flask.redirect(flask.url_for('authentificated'))
    else:
        http_auth = credentials.authorize(httplib2.Http())
        youtube = discovery.build('youtube', 'v3', http=http_auth)
        allPlaylists = youtube.playlists().list(
            part = "snippet",
            mine = True
        ).execute()
        playlists=[]
        for p in allPlaylists['items']:
            p=p['snippet']
            playlist={}
            playlist['thumbnail']=p['thumbnails']['high']['url']
            playlist['description']=p['localized']['description']
            playlist['title']=p['localized']['title']
            playlists.append(playlist)
            return render_template('playlists.html', playlists=playlists) 



if __name__ == "__main__":
    app.debug=True
    SESSION_TYPE='redis'
    Session(app)

    app.run(host='0.0.0.0', debug=True)

