"""GraphQL-запросы «Звука». Взяты из MIT-проекта derangedabbot/zvuk-yandex-station-player."""

SEARCH = r"""query search(
  $query: String
  $limit: Int = 2
  $tracks: Boolean = true
  $trackCursor: Cursor = null
  $artistsCursor: Cursor = null
  $releasesCursor: Cursor = null
  $playlistsCursor: Cursor = null
  $episodesCursor: Cursor = null
  $profilesCursor: Cursor = null
  $podcastsCursor: Cursor = null
  $artists: Boolean = true
  $releases: Boolean = true
  $playlists: Boolean = true
  $profiles: Boolean = true
  $books: Boolean = true
  $episodes: Boolean = true
  $podcasts: Boolean = true
) {
  search(query: $query) {
    searchId
    tracks(limit: $limit, cursor: $trackCursor) @include(if: $tracks) {
      page {
        total
        prev
        next
        cursor
      }
      score
      items {
        id
        title
        duration
        explicit
        artists {
          id
          title
          image {
            src
          }
        }
        release {
          id
          title
          date
          type
          image {
            src
          }
          explicit
          artists {
            id
            title
            image {
              src
            }
          }
        }
      }
    }
    artists(limit: $limit, cursor: $artistsCursor) @include(if: $artists) {
      page {
        total
        prev
        next
        cursor
      }
      score
      items {
        id
        title
        image {
          src
        }
      }
    }
    releases(limit: $limit, cursor: $releasesCursor) @include(if: $releases) {
      page {
        total
        prev
        next
        cursor
      }
      score
      items {
        id
        title
        date
        type
        image {
          src
        }
        explicit
        artists {
          id
          title
          image {
            src
          }
        }
      }
    }
    playlists(limit: $limit, cursor: $playlistsCursor)
      @include(if: $playlists) {
      page {
        total
        prev
        next
        cursor
      }
      score
      items {
        id
        title
        isPublic
        description
        duration
        image {
          src
        }
      }
    }
    profiles(limit: $limit, cursor: $profilesCursor) @include(if: $profiles) {
      page {
        total
        prev
        next
        cursor
      }
      score
      items {
        id
        name
        description
        image {
          src
        }
      }
    }
    books(limit: $limit) @include(if: $books) {
      page {
        total
        prev
        next
        cursor
      }
      score
      items {
        id
        title
        authorNames
      }
    }
    episodes(limit: $limit, cursor: $episodesCursor) @include(if: $episodes) {
      page {
        total
        prev
        next
        cursor
      }
      score
      items {
        id
        title
        availability
        explicit
        duration
        publicationDate
        image {
          src
        }
        podcast {
          id
          title
          explicit
          image {
            src
          }
          authors {
            id
            name
          }
        }
      }
    }
    podcasts(limit: $limit, cursor: $podcastsCursor) @include(if: $podcasts) {
      page {
        total
        prev
        next
        cursor
      }
      score
      items {
        id
        title
        explicit
        image {
          src
        }
        authors {
          id
          name
        }
      }
    }
  }
}"""

QUICK = r"""query quickSearch($query: String, $limit: Int, $searchSessionId: String) {
  quickSearch(query: $query, limit: $limit, searchSessionId: $searchSessionId) {
    searchSessionId
    content {
      __typename
      ... on Track {
        id
        title
        duration
        explicit
        artists {
          id
          title
          image {
            src
          }
        }
        release {
          id
          title
          date
          type
          image {
            src
          }
          explicit
          artists {
            id
            title
            image {
              src
            }
          }
        }
      }
      ... on Artist {
        id
        title
        image {
          src
        }
      }
      ... on Release {
        id
        title
        date
        type
        image {
          src
        }
        explicit
        artists {
          id
          title
          image {
            src
          }
        }
      }
      ... on Playlist {
        id
        title
        isPublic
        description
        duration
        image {
          src
        }
      }
      ... on Episode {
        id
        title
        availability
        publicationDate
        duration
        podcast {
          title
        }
        image {
          src
        }
      }
      ... on Podcast {
        id
        title
        explicit
        image {
          src
        }
        authors {
          id
          name
        }
      }
      ... on Profile {
        id
        name
        description
        image {
          src
        }
      }
      ... on Book {
        id
        title
        authorNames
      }
    }
  }
}"""

WAVE = r"""query getPersonalWave(
  $contentInput: PersonalWaveContentInput
  $first: PositiveInt! = 10
  $options: PersonalWaveOptions
  $waveInput: WaveInput
  $waveSrc: MagicSource
) {
  personalWaveContent(
    contentInput: $contentInput
    first: $first
    options: $options
    waveInput: $waveInput
    waveSrc: $waveSrc
  ) {
    id
    title
    duration
    explicit
    artists {
      id
      title
      image {
        src
      }
    }
    release {
      id
      title
      date
      image {
        src
      }
    }
  }
}"""

STREAM = r"""query getStream($ids: [ID!]!) {
  mediaContents(ids: $ids) {
    __typename
    ... on Track {
      stream {
        expire
        expireDelta
        flac
        flacdrm
        high
        mid
      }
    }
    ... on Episode {
      stream {
        expire
        expireDelta
        high
        mid
      }
    }
    ... on Chapter {
      stream {
        expire
        expireDelta
        high
        mid
      }
    }
  }
}"""

RELEASES = r"""query getReleases($ids: [ID!]!) {
  getReleases(ids: $ids) {
    id
    title
    type
    date
    image {
      src
    }
    artists {
      id
      title
      image {
        src
      }
    }
    tracks {
      id
      title
      duration
      explicit
      artists {
        id
        title
        image {
          src
        }
      }
      release {
        id
        title
        image {
          src
        }
      }
    }
  }
}"""

ARTISTS = r"""query getArtists(
  $ids: [ID!]!
  $withPopTracks: Boolean = true
  $withReleases: Boolean = false
  $tracksLimit: Int = 30
  $releasesLimit: Int = 50
) {
  getArtists(ids: $ids) {
    id
    title
    image {
      src
    }
    popularTracks(limit: $tracksLimit) @include(if: $withPopTracks) {
      id
      title
      duration
      explicit
      artists {
        id
        title
        image {
          src
        }
      }
      release {
        id
        title
        image {
          src
        }
      }
    }
    releases(limit: $releasesLimit) @include(if: $withReleases) {
      id
      title
      date
      type
      explicit
      image {
        src
      }
      artists {
        id
        title
      }
    }
  }
}"""

ADD_LIKE = r"""mutation addItemToCollection($id: ID, $type: CollectionItemType) {
  collection {
    addItem(id: $id, type: $type)
  }
}"""

REMOVE_LIKE = r"""mutation removeItemFromCollection($id: ID, $type: CollectionItemType) {
  collection {
    removeItem(id: $id, type: $type)
  }
}"""

ADD_HIDDEN = r"""mutation addItemToHidden($id: ID!, $type: CollectionItemType!) {
  hidden_collection {
    addItem(id: $id, type: $type)
  }
}"""

REMOVE_HIDDEN = r"""mutation removeItemFromHidden($id: ID!, $type: CollectionItemType!) {
  hidden_collection {
    removeItem(id: $id, type: $type)
  }
}"""

USER_TRACKS = r"""query userTracks(
  $orderBy: TrackOrderByType
  $orderDirection: OrderDirectionType
) {
  collection {
    tracks(orderBy: $orderBy, orderDirection: $orderDirection) {
      id
      collectionItemData {
        lastModified
      }
    }
  }
}"""

TRACKS = r"""query getTracks($ids: [ID!]!) {
  getTracks(ids: $ids) {
    id
    title
    duration
    explicit
    hasFlac
    artists {
      id
      title
      image {
        src
      }
    }
    release {
      id
      title
      date
      type
      image {
        src
      }
      explicit
      artists {
        id
        title
        image {
          src
        }
      }
    }
  }
}"""
