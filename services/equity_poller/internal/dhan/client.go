package dhan

import (
	"net/http"
	"time"
)

// Client wraps an HTTP client with Dhan API authentication headers.
type Client struct {
	HTTP        *http.Client
	BaseURL     string
	AccessToken string
}

// NewClient creates a Dhan API client.
func NewClient(baseURL, accessToken string) *Client {
	return &Client{
		HTTP: &http.Client{
			Timeout: 15 * time.Second,
		},
		BaseURL:     baseURL,
		AccessToken: accessToken,
	}
}

// NewRequest creates an http.Request with Dhan auth headers pre-set.
func (c *Client) NewRequest(method, path string, body interface{}) (*http.Request, error) {
	url := c.BaseURL + path

	var req *http.Request
	var err error

	if body != nil {
		// Caller is responsible for encoding body; this is a convenience for GET/DELETE.
		req, err = http.NewRequest(method, url, nil)
	} else {
		req, err = http.NewRequest(method, url, nil)
	}
	if err != nil {
		return nil, err
	}

	req.Header.Set("access-token", c.AccessToken)
	req.Header.Set("Content-Type", "application/json")

	return req, nil
}
