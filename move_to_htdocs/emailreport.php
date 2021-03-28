<?php
if(!isset($_POST['submit']))
{
	//This page should not be accessed directly. Need to submit the form.
	echo "error; you need to submit the form!";
}
$name = $_POST['name'];
$message = $_POST['message'];

//Validate first
if(empty($message)) 
{
    echo "Please provide your email...";
    exit;
}

if(IsInjected($message))
{
    echo "Bad email value!";
    exit;
}

$email_from = 'apache@scan.sai.msu.ru';//<== update the email address
$email_subject = "New message from $name";
$email_body = "$message\n";
$to = "kirx@kirx.net, astro.stas@gmail.com, smolyankina.olga@gmail.com, gudun.ku@gmail.com, alex.bo2018vrn@gmail.com";//<== update the email address
$headers = "From: $email_from \r\n";
//Send the email!
mail($to,$email_subject,$email_body,$headers);
//done. redirect to thank-you page.
header('Location: thank-you.html');


// Function to validate against any email injection attempts
function IsInjected($str)
{
//  $injections = array('(\n+)',
  $injections = array('(\r+)',
              '(\t+)',
              '(%0A+)',
              '(%0D+)',
              '(%08+)',
              '(%09+)'
              );
  $inject = join('|', $injections);
  $inject = "/$inject/i";
  if(preg_match($inject,$str))
    {
    return true;
  }
  else
    {
    return false;
  }
}
   
?> 