variable "region" {
  type    = string
  default = "us-east-1"
}

variable "bus_name" {
  type    = string
  default = "hockeytrack"
}

variable "schedule_url" {
  type    = string
  default = "https://hockeytrack.davidjdrake.com/data/schedule.json"
}

variable "alerts_topic_name" {
  type    = string
  default = "hockeytrack-alerts"
}

variable "admin_site_origin" {
  type        = string
  default     = "scoreboard.davidjdrake.com"
  description = "Host the admin site is served from. Used for CORS and Cognito callback URLs."
}
