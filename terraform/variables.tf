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
